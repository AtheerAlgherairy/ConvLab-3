import sys
sys.path.append('../../..')

import argparse
from tqdm import tqdm
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from transformers import GPT2Tokenizer, GPT2LMHeadModel
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from torch.utils.tensorboard import SummaryWriter
import os
from transformers import get_linear_schedule_with_warmup

from convlab2.util.unified_datasets_util import load_dataset, load_nlg_data, load_ontology
from convlab2.nlg.scgpt.util import act2str
from convlab2.nlg.scgpt.model import SCGPTDataset
from evaluate import GentScorer

# 分部式训练
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

from util import build_mask
from scgpt_special_tokens import *
# from plot.attn_plot import plot_attn_encdec

## Model Testing
code_test = False

## 多GPU并行参数设置
parser = argparse.ArgumentParser()
parser.add_argument("--local_rank", default=-1, type=int)
parser.add_argument('--do_train', action="store_true", help="Whether to run training.")
parser.add_argument('--dataset', default="multiwoz21", type=str, help="Whether to run training.")
parser.add_argument("--max_seq_len", default=256, type=int)
FLAGS = parser.parse_args()
local_rank = FLAGS.local_rank

torch.cuda.set_device(local_rank)
dist.init_process_group(backend='nccl')

# TensorBoard
tb_writer = SummaryWriter()

special_tokens = [START_OF_PRED, END_OF_PRED, SYS_SPEAK, USR_SPEAK]
## load model
tokenizer = GPT2Tokenizer.from_pretrained('./gpt2')
tokenizer.add_special_tokens({'pad_token': PAD_TOKEN, 'eos_token': END_OF_PRED, 'additional_special_tokens': special_tokens})
model = GPT2LMHeadModel.from_pretrained('./gpt2').to(local_rank)
model.resize_token_embeddings(len(tokenizer))

## loss计算
nll_loss = nn.NLLLoss(reduce=False).to(local_rank)
ce_loss = nn.CrossEntropyLoss(reduce=False).to(local_rank)
def cal_loss(input, target, seq_lens, seq_lens_input):
    """Only calculate loss on responses, not on dialog act"""
    global nll_loss
    """Input: [batch, length, vocab]; target: [batch, length]; seq_lens: [batch]"""
    log_probs = F.log_softmax(input, dim=-1).transpose(1, 2)  # 类别维度要放在dim=1的位置，nn.NLLLoss的要求
    loss = nll_loss(log_probs, target)
    # loss = ce_loss(input, target)  # 等价
    mask = build_mask(torch.max(seq_lens).item()-1, seq_lens-1).to(local_rank)
    input_mask = build_mask(torch.max(seq_lens).item()-1, seq_lens_input-1).to(local_rank)
    output_mask = torch.logical_xor(mask, input_mask)
    pad_mask = torch.logical_not(mask)
    # masked_loss = loss * output_mask
    masked_loss = loss * (output_mask + pad_mask)
    mean_loss = torch.sum(masked_loss) / torch.sum(output_mask + pad_mask)
    return mean_loss


def pad_collate(batch):
    """
    Returns:
    batch: batch * max_len
    seq_lens: the length of len(da)+1+len(response)
    seq_lens_input: the length of len(da)
    """
    START_OF_PRED_ID = tokenizer._convert_token_to_id_with_added_voc(START_OF_PRED)
    pad_token_id = tokenizer.pad_token_id
    batch = [item[0] + [START_OF_PRED_ID] + item[1] for item in batch]
    batch = [item[-FLAGS.max_seq_len:] for item in batch]  # TF限制输入长度
    max_len = max([len(item) for item in batch])
    seq_lens = [len(item) for item in batch]
    split_id = tokenizer._convert_token_to_id_with_added_voc(START_OF_PRED)
    def get_x_len(tokens):
        """Get the length of dialogue act tokens"""
        split_idx = len(tokens)
        try:
            split_idx = tokens.index(split_id)+1
        except:
            pass
        return split_idx
    seq_lens_input = [get_x_len(item) for item in batch]
    batch = [item + [pad_token_id]*(max_len-len(item)) for item in batch]
    return torch.LongTensor(batch), torch.LongTensor(seq_lens), torch.LongTensor(seq_lens_input)

## Training Hyper-params
EPOCH_NUM = 20
BATCH_SIZE = 20   # real_batch_size = BATCH_SIZE * num_gpu
VAL_STEP = 300
WARM_STEPS = 250
if code_test:
    EPOCH_NUM = 2
    BATCH_SIZE = 4
    VAL_STEP = 2
    WARM_STEPS = 3
LR = 5e-5
TASK_TYPE = 'nlu'  # nlu or dst
SAVE_PATH = f'./saved_model'
def train(model, nlg_data, global_step=0):
    train_dataset = SCGPTDataset(nlg_data['train'], tokenizer)
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, batch_size=BATCH_SIZE, num_workers=2, sampler=train_sampler, collate_fn=pad_collate)

    val_dataset = SCGPTDataset(nlg_data['validation'], tokenizer)
    val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
    val_dataloader = DataLoader(val_dataset, batch_size=BATCH_SIZE, num_workers=2, sampler=val_sampler, collate_fn=pad_collate)

    model = DDP(model, device_ids=[local_rank], output_device=local_rank)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LR)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=WARM_STEPS,
                                                num_training_steps=len(train_dataloader) * EPOCH_NUM)
    model.train()
    for epoch in range(EPOCH_NUM):
        train_dataloader.sampler.set_epoch(epoch)
        for batch_id, (inputs, seq_lens, seq_lens_input) in enumerate(tqdm(train_dataloader, desc=f'EPOCH:[{epoch+1}/{EPOCH_NUM}]')):
            inputs = inputs.to(local_rank)
            seq_lens = seq_lens.to(local_rank)
            seq_lens_input = seq_lens_input.to(local_rank)

            outputs = model(inputs)
            preds = outputs[0]
            loss = cal_loss(preds[:, :-1, :], inputs[:, 1:], seq_lens, seq_lens_input)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            tb_writer.add_scalar(f'Train/loss', loss.item(), global_step)
            tb_writer.add_scalar(f'Train/PPL', torch.exp(loss).item(), global_step)
            tb_writer.add_scalar(f'Train/Learning Rate', scheduler.get_last_lr()[0], global_step)

            if batch_id % VAL_STEP == 0:
                model.eval()
                val_loss = eval(model, val_dataloader)
                ppl = np.exp(val_loss)
                tb_writer.add_scalar(f'Val/Loss', val_loss, global_step)
                tb_writer.add_scalar(f'Val/PPL', ppl, global_step)
                model.train()
            global_step += 1
        # save the model when each epoch ends
        if dist.get_rank() == 0:
            save_dir = os.path.join(SAVE_PATH, f'epoch_{epoch}')
            os.makedirs(save_dir, exist_ok=True)
            torch.save(model.module.state_dict(), os.path.join(save_dir, f'epoch_{epoch}_step{global_step}.pt'))
            tokenizer.save_pretrained(save_dir)
            torch.save(optimizer.state_dict(), os.path.join(save_dir, 'optimizer.pt'))
            torch.save(scheduler.state_dict(), os.path.join(save_dir, 'scheduler.pt'))
            print(f'Save model checkpoint to [{save_dir}]')
    tb_writer.flush()


def eval(model, loader, use_tqdm=False):
    with torch.no_grad():
        loss_list = []
        iter = tqdm(loader, desc='Val') if use_tqdm else loader
        for inputs, seq_lens, seq_lens_input in iter:
            inputs = inputs.to(local_rank)
            seq_lens = seq_lens.to(local_rank)
            seq_lens_input = seq_lens_input.to(local_rank)
            outputs = model(inputs)
            preds = outputs[0]
            loss = cal_loss(preds[:, :-1, :], inputs[:, 1:], seq_lens, seq_lens_input)
            loss_list.append(loss.item())
        mean_loss = np.mean(loss_list)
    return mean_loss


def inference_batch(model, sents):
    """Inference model given a batch of sents."""
    with torch.no_grad():
        sents = [sent + ' ' + START_OF_PRED for sent in sents]
        sent_ids = [tokenizer.encode(sent) for sent in sents]
        max_len = max([len(sent) for sent in sent_ids])
        sent_ids = [sent + [tokenizer.pad_token_id]*(max_len-len(sent)) for sent in sent_ids]
        inputs = torch.LongTensor(sent_ids).to(local_rank)
        model_to_run = model.module if type(model) is DDP else model
        outputs = model_to_run.generate(inputs, max_length=FLAGS.max_seq_len, eos_token_id=tokenizer.pad_token_id,
                                        pad_token_id=tokenizer.pad_token_id)  # greedy
        # outputs = model_to_run.generate(inputs, num_beams=4, max_length=513, eos_token_id=gpt2_tokenizer.eos_token_id,
        #                                 pad_token_id=gpt2_tokenizer.pad_token_id)  # beam search
        output_strs = [tokenizer.decode(item) for item in outputs]
        return output_strs


def inference_sent(model, sent):
    """Inference model given one single sentence."""
    return inference_batch(model, [sent])[0]


def inference_sents(model, sents):
    """Get the outputs of multiple sentences."""
    outputs = []
    for sent in tqdm(sents, desc='Inference Sentences'):
        output = inference_sent(model, sent)
        outputs.append(output)
    return outputs


def test(model, nlg_data, ontology, model_path):
    """将sheel中的GPU个数设为1运行"""
    model.load_state_dict(torch.load(model_path))
    model.eval()
    print(f'model loaded from [{model_path}]')
    # sample_file = os.path.join(f'../../../data/dstc2/sample50_{TASK_TYPE}_input_data.txt')
    # Load test nlg data
    test_data = nlg_data['test']
    dialog_acts = [act2str(item['dialogue_acts']) for item in test_data]
    golden_responses = [item['utterance'] for item in test_data]
    outputs = inference_sents(model, dialog_acts)
    if dist.get_rank() == 0:
        output_file = './test_output.txt'
        with open(output_file, 'w+') as f:
            for i in range(len(dialog_acts)):
                f.write(f'{dialog_acts[i]}\n{golden_responses[i]}\n{outputs[i]}\n\n')
            f.close()
    evaluator = GentScorer()
    parallel_corpus = []
    # BLEU
    for i in range(len(dialog_acts)):
        parallel_corpus.append([[golden_responses[i]], [outputs[i]]])
    BLEU_Score = evaluator.scoreSBLEU(parallel_corpus)
    # ERR
    ## all values in ontology
    val2ds_dict = {}
    for domain_name in ontology['domains']:
        domain = ontology['domains'][domain_name]
        for slot_name in domain['slots']:
            slot = domain['slots'][slot_name]
            possible_vals = slot['possible_values']
            if len(possible_vals) > 0:
                for val in possible_vals:
                    val2ds_dict[val] = f'{domain_name}-{slot_name}'
    ## missing values
    score_list = []
    for item in nlg_data:
        da = item['dialogue_acts']
        utterance = item['utterance']
        missing_count = 0
        redundant_count = 0
        all_count = 0
        all_values = set()
        for key in da:
            slot_value = da[key]
            for triple in slot_value:
                if 'value' in triple:
                    value = triple['value']
                    all_values.add(value)
                    if value.strip().lower() not in utterance.lower():
                        missing_count += 1
                    all_count += 1
        ## redundant values
        for val in val2ds_dict:
            if f' {val.strip().lower()} ' in f' {utterance.strip().lower()} ' and val.strip().lower() not in all_values:
                redundant_count += 1
        item_score = float(redundant_count + all_count) / all_count
        score_list.append(item_score)
    ERR_Score = np.mean(score_list)
    print(f'BLEU: {BLEU_Score}\nERR_Score: {ERR_Score}')


if __name__ == '__main__':
    dataset = load_dataset(FLAGS.dataset)
    ontology = load_ontology(FLAGS.dataset)
    nlg_data = load_nlg_data(dataset)
    if FLAGS.do_train:
        train(model, nlg_data)
    else:
        test(model, nlg_data, ontology, './saved_model/epoch_0/epoch_0_step2839.pt')
