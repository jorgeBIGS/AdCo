import argparse
import builtins
import math
import os
import random
import shutil
import time
import warnings
import numpy as np

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models

from training.train_utils import AverageMeter, ProgressMeter, accuracy

def update_network(model,images,args,Memory_Bank,losses,top1,top5,optimizer,criterion,mem_losses):
    # update network
    # negative logits: NxK

    q, _, l_pos = model(im_q=images[0], im_k=images[1])

    d_norm, d, l_neg = Memory_Bank(q, update_mem=False)

    # logits: Nx(1+K)

    logits = torch.cat([l_pos, l_neg], dim=1)
    logits /= args.moco_t

    cur_batch_size = logits.shape[0]
    cur_gpu = args.gpu
    choose_match = cur_gpu * cur_batch_size
    labels = torch.arange(choose_match, choose_match + cur_batch_size, dtype=torch.long).cuda()
    total_bsize=logits.shape[1]-args.cluster
    loss = criterion(logits, labels)

    # acc1/acc5 are (K+1)-way contrast classifier accuracy
    # measure accuracy and record loss
    acc1, acc5 = accuracy(logits, labels, topk=(1, 5))
    losses.update(loss.item(), images[0].size(0))
    top1.update(acc1[0], images[0].size(0))
    top5.update(acc5[0], images[0].size(0))

    # compute gradient and do SGD step
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    # update memory bank
    with torch.no_grad():
        logits = torch.cat([l_pos, l_neg], dim=1) / args.mem_t
        p_qd=nn.functional.softmax(logits, dim=1)[:,total_bsize:]
        g = torch.einsum('cn,nk->ck',[q.T,p_qd])/logits.shape[0] - torch.mul(torch.mean(torch.mul(p_qd,l_neg),dim=0),d_norm)
        g = -torch.div(g,torch.norm(d,dim=0))/args.mem_t # c*k
        g = all_reduce(g) / torch.distributed.get_world_size()
        Memory_Bank.v.data = args.momentum * Memory_Bank.v.data + g + args.mem_wd * Memory_Bank.W.data
        Memory_Bank.W.data = Memory_Bank.W.data - args.memory_lr * Memory_Bank.v.data
    logits=torch.softmax(logits,dim=1)
    batch_prob=torch.sum(logits[:,:logits.size(0)],dim=1)
    batch_prob=torch.mean(batch_prob)
    mem_losses.update(batch_prob.item(),logits.size(0))
    return l_neg,logits

def update_sym_network(model,images,args,Memory_Bank,losses,top1,top5,optimizer,criterion,mem_losses):
    # update network
    # negative logits: NxK
    model.zero_grad()
    q, k, l_pos1, l_pos2, _ = model(im_q=images[0], im_k=images[1])
    d_norm1, d1, l_neg1 = Memory_Bank(q, update_mem=False)
    d_norm2, d2, l_neg2 = Memory_Bank(k, update_mem=False)
    # logits: Nx(1+K)

    logits1 = torch.cat([l_pos1, l_neg1], dim=1)
    logits1 /= args.moco_t
    logits2 = torch.cat([l_pos2, l_neg2], dim=1)
    logits2 /= args.moco_t

    cur_batch_size=logits1.shape[0]
    cur_gpu=args.gpu
    choose_match=cur_gpu*cur_batch_size
    labels=torch.arange(choose_match,choose_match+cur_batch_size,dtype=torch.long).cuda()

    loss = 0.5*criterion(logits1, labels)+0.5*criterion(logits2, labels)


    # acc1/acc5 are (K+1)-way contrast classifier accuracy
    # measure accuracy and record loss
    acc1, acc5 = accuracy(logits1, labels, topk=(1, 5))
    losses.update(loss.item(), images[0].size(0))
    top1.update(acc1[0], images[0].size(0))
    top5.update(acc5[0], images[0].size(0))
    acc1, acc5 = accuracy(logits2, labels, topk=(1, 5))
    losses.update(loss.item(), images[0].size(0))
    top1.update(acc1[0], images[0].size(0))
    top5.update(acc5[0], images[0].size(0))

    # compute gradient and do SGD step
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    # update memory bank
    with torch.no_grad():
        # update memory bank

        # logits: Nx(1+K)

        logits1 = torch.cat([l_pos1, l_neg1], dim=1)
        logits1 /= args.mem_t
        # negative logits: NxK
        # logits: Nx(1+K)

        logits2 = torch.cat([l_pos2, l_neg2], dim=1)
        logits2 /= args.mem_t
        total_bsize = logits1.shape[1] - args.cluster
        p_qd1 = nn.functional.softmax(logits1, dim=1)[:, total_bsize:]
        g1 = torch.einsum('cn,nk->ck', [q.T, p_qd1]) / logits1.shape[0] - torch.mul(
            torch.mean(torch.mul(p_qd1, l_neg1), dim=0), d_norm1)
        p_qd2 = nn.functional.softmax(logits2, dim=1)[:, total_bsize:]
        g2 = torch.einsum('cn,nk->ck', [k.T, p_qd2]) / logits2.shape[0] - torch.mul(
            torch.mean(torch.mul(p_qd2, l_neg2), dim=0), d_norm1)
        g = -torch.div(g1, torch.norm(d1, dim=0)) / args.mem_t - torch.div(g2,
                                                                           torch.norm(d1, dim=0)) / args.mem_t  # c*k
        g = all_reduce(g) / torch.distributed.get_world_size()
        Memory_Bank.v.data = args.momentum * Memory_Bank.v.data + g + args.mem_wd * Memory_Bank.W.data
        Memory_Bank.W.data = Memory_Bank.W.data - args.memory_lr * Memory_Bank.v.data
        logits1 = torch.softmax(logits1, dim=1)
        batch_prob1 = torch.sum(logits1[:, :logits1.size(0)], dim=1)
        logits2 = torch.softmax(logits2, dim=1)
        batch_prob2 = torch.sum(logits2[:, :logits2.size(0)], dim=1)
        batch_prob = 0.5 * torch.mean(batch_prob1) + 0.5 * torch.mean(batch_prob2)
        mem_losses.update(batch_prob.item(), logits1.size(0))
    return l_neg1,logits1

def train(train_loader, model,Memory_Bank, criterion,
                                optimizer,epoch, args):

    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    mem_losses = AverageMeter('MemLoss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, mem_losses, top1, top5],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()
    for i, (images, _) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        if args.gpu is not None:
            images[0] = images[0].cuda(args.gpu, non_blocking=True)
            images[1] = images[1].cuda(args.gpu, non_blocking=True)
        batch_size=images[0].size(0)
        if not args.sym:
            update_network(model, images, args, Memory_Bank, losses, top1, top5, optimizer, criterion,mem_losses)
        else:
            update_sym_network(model, images, args, Memory_Bank, losses, top1, top5,
                                               optimizer, criterion, mem_losses)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)
    return top1.avg

def init_memory(train_loader, model,Memory_Bank, criterion,
                                optimizer,epoch, args):
    # switch to train mode
    model.train()
    for i, (images, _) in enumerate(train_loader):
        # measure data loading time
        if args.gpu is not None:
            images[0] = images[0].cuda(args.gpu, non_blocking=True)
            images[1] = images[1].cuda(args.gpu, non_blocking=True)
        
        # compute output
        if args.sym == 0:
            q, k, l_pos  = model(im_q=images[0], im_k=images[1])
        else:
            q, _, l_pos, _, k  = model(im_q=images[0], im_k=images[1])
        d_norm, d, l_neg = Memory_Bank(q, update_mem=False)

        # logits: Nx(1+K)

        logits = torch.cat([l_pos, l_neg], dim=1)
        logits /= args.moco_t

        cur_batch_size = logits.shape[0]
        cur_gpu = args.gpu
        choose_match = cur_gpu * cur_batch_size
        labels = torch.arange(choose_match, choose_match + cur_batch_size, dtype=torch.long).cuda()
        loss = criterion(logits, labels)
        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        
        acc1, acc5 = accuracy(logits, labels, topk=(1, 5))
        if i % args.print_freq == 0:
            print(acc1, acc5)

        # fill the memory bank
        output = k
        batch_size = output.size(0)
        start_point = i * batch_size
        end_point = min((i + 1) * batch_size, args.cluster)
        Memory_Bank.W.data[:, start_point:end_point] = output[:end_point - start_point].T
        if (i+1) * batch_size >= args.cluster:
            break

@torch.no_grad()
def all_reduce(tensor):
    """
    Performs all_reduce(mean) operation on the provided tensors.
    *** Warning ***: torch.distributed.all_reduce has no gradient.
    """
    torch.distributed.all_reduce(tensor, async_op=False)

    return tensor

@torch.no_grad()
def concat_all_gather(tensor):
    """
    Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    tensors_gather = [torch.ones_like(tensor)
        for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output