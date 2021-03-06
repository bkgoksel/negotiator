# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""
Reinforcement learning via Policy Gradient (REINFORCE).
"""

import argparse
import pdb
import random
import re
import time

import numpy as np
import torch
from torch import optim
from torch import autograd
import torch.nn as nn

import data
import utils
from engine import Engine
from utils import ContextGenerator
from agent import LstmAgent, LstmRolloutAgent, RlAgent, DumbAgent, BatchedRolloutAgent
from template_agent import TemplateAgent
from dialog import Dialog, DialogLogger
from models.mute import MuteModel
from models.simple_mute import SimpleProposerModule


class Reinforce(object):
    """Facilitates a dialogue between two agents and constantly updates them."""
    def __init__(self, dialog, ctx_gen, args, engine, corpus, device_id, logger=None):
        self.dialog = dialog
        self.ctx_gen = ctx_gen
        self.args = args
        self.engine = engine
        self.corpus = corpus
        self.logger = logger if logger else DialogLogger()
        self.device_id = device_id

    def run(self):
        """Entry point of the training."""
        validset, validset_stats = self.corpus.valid_dataset(self.args.bsz,
            device_id=self.device_id)
        trainset, trainset_stats = self.corpus.train_dataset(self.args.bsz,
            device_id=self.device_id)
        N = len(self.corpus.word_dict)

        n = 0
        for ctxs in self.ctx_gen.iter(self.args.nepoch):
            n += 1
            # supervised update
            if self.args.sv_train_freq > 0 and n % self.args.sv_train_freq == 0 and self.engine is not None:
                self.engine.train_single(N, trainset)

            self.logger.dump('=' * 80)
            # run dialogue, it is responsible for reinforcing the agents
            self.dialog.run(ctxs, self.logger)
            self.logger.dump('=' * 80)
            self.logger.dump('')
            if n % 100 == 0:
                self.logger.dump('%d: %s' % (n, self.dialog.show_metrics()), forced=True)

        def dump_stats(dataset, stats, name):
            loss, select_loss = self.engine.valid_pass(N, dataset, stats)
            self.logger.dump('final: %s_loss %.3f %s_ppl %.3f' % (
                name, float(loss), name, np.exp(float(loss))),
                forced=True)
            self.logger.dump('final: %s_select_loss %.3f %s_select_ppl %.3f' % (
                name, float(select_loss), name, np.exp(float(select_loss))),
                forced=True)

        if self.engine is not None:
            dump_stats(trainset, trainset_stats, 'train')
            dump_stats(validset, validset_stats, 'valid')

        self.logger.dump('final: %s' % self.dialog.show_metrics(), forced=True)


def main():
    parser = argparse.ArgumentParser(description='Reinforce')
    parser.add_argument('--data', type=str, default='./data/negotiate',
        help='location of the data corpus')
    parser.add_argument('--nembed_word', type=int, default=256,
        help='size of word embeddings')
    parser.add_argument('--nembed_ctx', type=int, default=64,
        help='size of context embeddings')
    parser.add_argument('--nreader', type=int, default=64,
        help='hidden size of the reader GRU')
    parser.add_argument('--nproposer', type=int, default=64,
        help='hidden size of the proposer GRU')
    parser.add_argument('--nhid_lang', type=int, default=256,
        help='size of the hidden state for the language module')
    parser.add_argument('--nhid_ctx', type=int, default=64,
        help='size of the hidden state for the context module')
    parser.add_argument('--nhid_strat', type=int, default=64,
        help='size of the hidden state for the strategy module')
    parser.add_argument('--nhid_attn', type=int, default=64,
        help='size of the hidden state for the attention module')
    parser.add_argument('--nhid_sel', type=int, default=64,
        help='size of the hidden state for the selection module')
    parser.add_argument('--unk_threshold', type=int, default=20,
        help='minimum word frequency to be in dictionary')
    parser.add_argument('--dropout', type=float, default=0.5,
        help='dropout rate in embedding layer')
    parser.add_argument('--sel_weight', type=float, default=1.0,
        help='selection weight')
    parser.add_argument('--init_range', type=float, default=0.1,
        help='initialization range')
    parser.add_argument('--rnn_ctx_encoder', action='store_true', default=False,
        help='wheather to use RNN for encoding the context')
    parser.add_argument('--max_epoch', type=int, default=30,
        help='max number of epochs')
    parser.add_argument('--dumb_alice', action='store_true', default=False,
        help='MAGA: make Alice great again')
    parser.add_argument('--alice_model_file', type=str,
        help='Alice model file')
    parser.add_argument('--bob_model_file', type=str,
        help='Bob model file')
    parser.add_argument('--output_model_file', type=str,
        help='output model file')
    parser.add_argument('--context_file', type=str,
        help='context file')
    parser.add_argument('--temperature', type=float, default=1.0,
        help='temperature')
    parser.add_argument('--cuda', action='store_true', default=False,
        help='use CUDA')
    parser.add_argument('--verbose', action='store_true', default=False,
        help='print out converations')
    parser.add_argument('--seed', type=int, default=1,
        help='random seed')
    parser.add_argument('--score_threshold', type=int, default=6,
        help='successful dialog should have more than score_threshold in score')
    parser.add_argument('--log_file', type=str, default='',
        help='log successful dialogs to file for training')
    parser.add_argument('--smart_bob', action='store_true', default=False,
        help='make Bob smart again')
    parser.add_argument('--gamma', type=float, default=0.99,
        help='discount factor')
    parser.add_argument('--eps', type=float, default=0.5,
        help='eps greedy')
    parser.add_argument('--nesterov', action='store_true', default=False,
        help='enable nesterov momentum')
    parser.add_argument('--momentum', type=float, default=0.0,
        help='momentum for sgd')
    parser.add_argument('--lr', type=float, default=0.1,
        help='learning rate')
    parser.add_argument('--clip', type=float, default=0.1,
        help='gradient clip')
    parser.add_argument('--rl_lr', type=float, default=0.2,
        help='RL learning rate')
    parser.add_argument('--rl_clip', type=float, default=0.1,
        help='RL gradient clip')
    parser.add_argument('--ref_text', type=str,
        help='file with the reference text')
    parser.add_argument('--bsz', type=int, default=8,
        help='batch size')
    parser.add_argument('--rollout_bsz', type=int, default=8,
        help='rollout batch size')
    parser.add_argument('--rollout_count_threshold', type=int, default=10,
        help='rollout batch size')
    parser.add_argument('--sv_train_freq', type=int, default=-1,
        help='supervision train frequency')
    parser.add_argument('--nepoch', type=int, default=4,
        help='number of epochs')
    parser.add_argument('--visual', action='store_true', default=False,
        help='plot graphs')
    parser.add_argument('--domain', type=str, default='object_division',
        help='domain for the dialogue')
    parser.add_argument('--super_dumb', action='store_true', default=False,
        help='this is the super dumb model that was our only successful model to run* *this has not yet been achieved')
    args = parser.parse_args()

    device_id = utils.use_cuda(args.cuda)
    run = "python3 reinforce.py --alice_model_file='./models/sv_model.th' --bob_model_file='./models/sv_model.th' --output_model_file='output.th' --verbose --ref_text='./data/negotiate/train.txt' --dumb_alice --context_file='./data/negotiate/selfplay.txt'"
    corpus = data.WordCorpus(args.data, freq_cutoff=args.unk_threshold, verbose=True)
    
    if args.super_dumb:
        # alice_model = SimpleProposerModule()
        # alice_model.eval()
        alice = TemplateAgent(args.domain, corpus.word_dict, args, name='Alice')
    elif args.dumb_alice:
        alice_model = MuteModel(corpus.word_dict, corpus.item_dict, corpus.context_dict, corpus.output_length, args, device_id)
        # we don't want to use Dropout during RL
        alice_model.eval()
        # Alice is a RL based agent, meaning that she will be learning while selfplaying
        alice = DumbAgent(alice_model, args, name='Alice')
# DUMB ALICE
    else:
        alice_model = utils.load_model(args.alice_model_file)
        # we don't want to use Dropout during RL
        alice_model.eval()
        # Alice is a RL based agent, meaning that she will be learning while selfplaying
        alice = RlAgent(alice_model, args, name='Alice')

    # we keep Bob frozen, i.e. we don't update his parameters
    bob_ty = LstmRolloutAgent if args.smart_bob else LstmAgent
    bob_model = utils.load_model(args.bob_model_file)
    bob_model.eval()
    bob = bob_ty(bob_model, args, name='Bob')

    dialog = Dialog([alice, bob], args)
    logger = DialogLogger(verbose=args.verbose, log_file=args.log_file)
    ctx_gen = ContextGenerator(args.context_file)

    engine = None if args.super_dumb else Engine(alice_model, args, device_id, verbose=False)

    reinforce = Reinforce(dialog, ctx_gen, args, engine, corpus, device_id, logger)
    reinforce.run()

    utils.save_model(alice.model, args.output_model_file)

if __name__ == '__main__':
    main()
