import logging
import typing
from typing import Optional, Tuple, Union, Iterable

import torch
import torch.nn as nn
import torch.nn.functional as F
from erutils.lightning import build_alibi_tensor

from utils.utils import HyperParameters
from .commons import MultiHeadBlock, CasualBlock, Decoder, Encoder, PGTBlock, Conv1D, CC_PGT_Block
from .cross_modules import LLmPConfig
from .modeling_LLmP import LLmPBlock, PMSNorm

logger = logging.getLogger(__name__)

__all__ = ['PTTDecoder', 'PTT', 'PTTGenerative', 'PGT', 'PGT_J', 'LLmP', 'LLmPBlock', 'LLmPConfig']


class Tokenizer:
    eos = '<|endoftext|>'
    pad = '<|endoftext|>'
    sos = '<|startoftext|>'


class PTTDecoder(nn.Module):
    def __init__(self, vocab_size: int, number_of_layers: int, number_of_embedded: int, head_size: int,
                 number_of_head: int,
                 chunk_size: int

                 ):

        super(PTTDecoder, self).__init__()

        self.vocab_size = vocab_size
        self.chunk = chunk_size
        self.head_size = head_size

        self.token_embedding = nn.Embedding(vocab_size, number_of_embedded)
        self.position_embedding = nn.Embedding(chunk_size, number_of_embedded)

        self.blocks = nn.Sequential(
            *[MultiHeadBlock(number_of_embedded=number_of_embedded,
                             number_of_head=number_of_head) for _
              in range(number_of_layers)])

        self.ln_f = nn.LayerNorm(number_of_embedded)  # final layer norm
        self.lm_head = nn.Linear(number_of_embedded, vocab_size)
        self.token_embedding.weight = self.lm_head.weight

    def forward(self, idx, targets: Optional[torch.Tensor] = None):
        B, T = idx.shape

        tok_emb = self.token_embedding(idx)
        pos_emb = self.position_embedding(torch.arange(T, device=idx.device))

        x = pos_emb + tok_emb
        x = self.blocks(x, x, x)
        x = self.ln_f(x)

        logits = self.lm_head(x)

        if targets is not None:
            B, T, C = logits.shape
            tokens = logits.view(B * T, C)
            targets = targets.view(-1)
            loss = F.cross_entropy(tokens, targets)
        else:
            loss = None
        return logits, loss

    @torch.no_grad()
    def generate(self, idx, max_new_tokens):
        for _ in range(max_new_tokens):
            idx_cond = idx[:, -self.chunk:]

            token, loss = self(idx_cond)

            token = token[:, -1, :]
            probs = F.softmax(token, dim=-1)

            idx_next = torch.multinomial(probs, num_samples=1)

            idx = torch.cat([idx, idx_next], 1)

        return idx


class PTTCasualHeadAttention(nn.Module):
    def __init__(self, vocab_size: int, number_of_head: int, number_of_embedded: int, number_of_layers: int,
                 chunk_size: int):
        super(PTTCasualHeadAttention, self).__init__()
        self.number_of_head = number_of_head
        self.number_of_embedded = number_of_embedded
        self.number_of_layers = number_of_layers

        self.m = nn.ModuleDict(
            dict(
                wt=nn.Embedding(vocab_size, number_of_embedded),
                wp=nn.Embedding(chunk_size, number_of_embedded),
                dropout=nn.Dropout(0.2),
                h=nn.ModuleList(
                    [CasualBlock(number_of_embedded=number_of_embedded, number_of_head=number_of_head) for _ in
                     range(number_of_layers)]),

                ln_f=nn.LayerNorm(number_of_embedded)
            )
        )
        self.ll = nn.Linear(number_of_embedded, vocab_size)
        self.m.wt.weight = self.ll.weight

    def forward(self, x, targets: typing.Optional[torch.Tensor] = None):
        device = x.device
        B, T = x.shape
        token = self.m.wt(x)
        pos = self.n.wp(torch.arange(T, dtype=torch.long if device == 'cuda' else torch.int).to(device))

        x = self.m.dropout(token + pos)
        for block in self.m.h:
            x = block(x)
        x = self.m.ln_f(x)
        if targets is not None:
            logits = self.ll(x)
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
        else:
            logits = self.ll(x[:, [-1], :])
            loss = None
        return logits, loss

    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None):

        for _ in range(max_new_tokens):

            idx_cond = idx if idx.size(1) <= self.config.block_size else idx[:, -self.config.block_size:]

            logits, _ = self(idx_cond)

            logits = logits[:, -1, :] / temperature

            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float('Inf')

            probs = F.softmax(logits, dim=-1)

            idx_next = torch.multinomial(probs, num_samples=1)

            idx = torch.cat((idx, idx_next), dim=1)

        return idx


class PTT(nn.Module):
    def __init__(self, vocab_size: int, max_length: int, embedded: int, number_of_heads: int, number_of_layers: int,
                 pad_index: int):
        super(PTT, self).__init__()
        self.enc = Encoder(vocab_size, max_length, embedded, number_of_heads, number_of_layers)
        self.dec = Decoder(vocab_size, max_length, embedded, number_of_heads, number_of_layers)
        self.fc = nn.Linear(embedded, vocab_size)
        self.pad_index = pad_index

    def forward_encoder(self, x, enc_out, src_mask, trg_mask):
        return self.dec(x, enc_out, src_mask, trg_mask)

    def forward_decoder(self, x, src_mask):
        return self.enc(x, src_mask)

    def make_mask_src(self, x):
        c = (x != self.pad_index).unsqueeze(0)
        c = c.float().masked_fill(c == 0, float('-inf')).masked_fill(c == 1, float(0.0))
        return c.to(x.device)

    def make_mask_trg(self, trg):
        trg_pad_mask = (trg != self.pad_index).unsqueeze(1)

        trg_len = trg.shape[1]

        trg_sub_mask = torch.tril(torch.ones((trg_len, trg_len), device=trg.device)).bool()

        trg_mask = trg_pad_mask & trg_sub_mask

        return trg_mask.to(trg.device)

    def forward(self, src, trg, src_mask=None, trg_mask=None):
        if trg_mask is None:
            trg_mask = self.make_mask_trg(trg)
        if src_mask is None:
            src_mask = self.make_mask_src(src)
        # x, src_mask
        # print(f'SRC : {src.shape}')
        enc = self.enc(src, src_mask)
        # x, enc_out, src_mask, trg_mask
        dec = self.dec(trg, enc, src_mask, trg_mask)
        pred = self.fc(dec)
        return pred


class PTTGenerative(nn.Module):
    def __init__(self, vocab_size: int, chunk: int, embedded: int, number_of_heads: int, number_of_layers: int,
                 pad_index: int, eos: int):
        super(PTTGenerative, self).__init__()
        self.chunk = chunk
        self.eos = eos
        self.enc = Encoder(vocab_size, chunk, embedded, number_of_heads, number_of_layers)
        self.dec = Decoder(vocab_size, chunk, embedded, number_of_heads, number_of_layers)
        self.fc = nn.Linear(embedded, vocab_size)
        self.pad_index = pad_index

    def forward_encoder(self, x, src_mask):
        return self.dec(x, src_mask)

    def make_mask_trg(self, trg):
        # print(src.shape)
        # trg_pad_mask = (trg != self.pad_index).unsqueeze(1).unsqueeze(2)
        # trg_len = trg.shape[1]
        # trg_sub_mask = torch.tril(torch.ones((trg_len, trg_len), device=trg.device)).bool()
        # trg_mask = trg_pad_mask & trg_sub_mask

        trg_pad_mask = (trg != self.pad_index).unsqueeze(1).unsqueeze(2).bool()
        sq_len = trg.shape[1]
        trg_sub_mask = torch.tril(torch.ones((sq_len, sq_len), device=trg.device)).bool()
        trg_mask = trg_pad_mask & trg_sub_mask

        return trg_mask.to(trg.device)

    def make_mask_src(self, x):

        c = (x != self.pad_index).unsqueeze(1).unsqueeze(2)
        c = c.float().masked_fill(c == 0, float('-inf')).repeat(1, 1, x.shape[1], 1)

        return c.to(x.device)

    def forward(self, src, trg, target=None):
        global b
        if len(src.shape) == 3:
            b, t, c = src.shape
        else:
            b = src.shape[0]
        src_mask = self.make_mask_src(src)
        trg_mask = self.make_mask_trg(trg)
        enc = self.enc(src, src_mask)

        pred = self.dec(trg, enc, src_mask, trg_mask)

        if target is not None:
            pred = self.fc(pred)
            # print(pred.shape)
            target = target.reshape(b, -1)
            pred_l = pred.view(b, -1, pred.size(-1))
            # pred_l = F.softmax(pred_l.permute(0, 2, 1), dim=-1)
            loss = 0
            for i in range(b):
                loss += F.cross_entropy(pred_l[i], target[i], ignore_index=self.pad_index)
        else:
            pred = self.fc(pred[:, [-1], :])
            loss = None
        return pred, loss

    @torch.no_grad()
    def generate(self, src, idx, trg=None, temp=1.0):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)

        for i in range(idx.shape[-1] - 1):
            idx = idx[:, -self.chunk:]
            pred, _ = self.forward(src, idx, target=trg)
            pred = pred[:, -1, :] / temp

            pred = F.softmax(pred, dim=-1)
            next_index = torch.multinomial(pred, 1)

            index = (i + 1) % self.chunk

            idx[:, index] = next_index

            if next_index == self.eos:
                break
        return idx


class PGT(nn.Module):
    def __init__(self, config: HyperParameters):
        super().__init__()

        self.embed_dim = config.num_embedding

        self.wte = nn.Embedding(config.vocab_size, self.embed_dim).cuda()
        self.wpe = nn.Embedding(config.chunk, self.embed_dim).cuda()
        self.chunk = config.chunk
        self.drop = nn.Dropout(config.embedded_dropout)
        # self.h = nn.ModuleList(
        #     [PGTBlock(config, layer_idx_1=i, layer_idx_2=i + 1) for i in range(0, config.num_layers * 2, 2)])
        self.h = nn.ModuleList(
            [PGTBlock(config, layer_idx_1=i) for i in range(config.num_layers)])
        self.ln_f = nn.LayerNorm(self.embed_dim)
        # self.fc = Conv1D(self.embed_dim, config.vocab_size)
        self.fc = nn.Linear(self.embed_dim, config.vocab_size, bias=True)
        # Model parallel
        self.model_parallel = False
        self.device_map = None
        self.gradient_checkpointing = False
        self.config = config
        self.pad_token_idx = config.pad_token_id
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)
        elif isinstance(module, Conv1D):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)

    def make_attention_mask(self, inp):
        return inp != self.pad_token_idx

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, new_embeddings):
        self.wte = new_embeddings

    def configure_optimizer(self, config):
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, Conv1D)
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for nm1, p1 in self.named_modules():
            for nm2, p2 in p1.named_parameters():
                fpn = '%s.%s' % (nm1, nm2) if nm1 else nm2

                if nm2.endswith('bias'):
                    no_decay.add(fpn)
                elif nm2.endswith('weight') and isinstance(p1, whitelist_weight_modules):
                    decay.add(fpn)
                elif nm2.endswith('weight') and isinstance(p1, blacklist_weight_modules):
                    no_decay.add(fpn)

        param_dict = {pn: p for pn, p in self.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert len(inter_params) == 0, "parameters %s made it into both decay/no_decay sets!" % (str(inter_params),)
        assert len(
            param_dict.keys() - union_params) == 0, "parameters %s were not separated into either decay/no_decay set!" \
                                                    % (str(param_dict.keys() - union_params),)

        optim_groups = [
            {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": config.weight_decay},
            {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0},
        ]
        optimizer = torch.optim.AdamW(optim_groups, lr=config.lr)
        return optimizer

    def forward(self,
                inputs: typing.Optional[torch.LongTensor],
                attention_mask: Optional[torch.FloatTensor] = None,
                heads_mask: Optional[torch.FloatTensor] = None):

        if self.config.create_attention_mask:
            print('ay you why do you do that ?')
            attention_mask = self.make_attention_mask(inputs)
        if attention_mask is not None:
            attention_mask = attention_mask.type(torch.float32)
            attention_mask = (1.0 - attention_mask) * torch.finfo(attention_mask.dtype).min

        token_embeddings = self.wte(inputs)

        pos_embeddings = self.wpe(torch.arange(0, inputs.size(-1), dtype=inputs.dtype, device=inputs.device))

        hidden = self.drop(token_embeddings + pos_embeddings)
        for m in self.h:
            hidden = m(hidden, attention_mask=attention_mask, heads_mask=heads_mask)
        hidden = self.fc(self.ln_f(hidden))
        return hidden

    @torch.no_grad()
    def generate(self, idx, generate=5000, temp=1, eos: int = 2, attention_mask=None):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)
        for _ in range(generate):
            idx = idx[:, -self.chunk:]
            pred = self.forward(idx, attention_mask=attention_mask)
            pred = pred[:, -1, :] / temp
            pred = F.softmax(pred, dim=-1)
            next_index = torch.multinomial(pred, 1)
            idx = torch.cat([idx, next_index], 1)
            if next_index[0] == eos:
                break
        return idx

    @torch.no_grad()
    def generate_ca(self, idx, temp=1, attention_mask=None):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)
        idx = idx[:, -self.chunk:]
        pred = self.forward(idx, attention_mask=attention_mask)
        pred = pred[:, -1, :] / temp
        pred = F.softmax(pred, dim=-1)
        next_index = torch.multinomial(pred, 1)
        idx = torch.cat([idx, next_index], 1)
        return idx


class CC_PGT(nn.Module):
    def __init__(self, config, apply_init: bool = True):
        super().__init__()

        self.embed_dim = config.hidden_size

        self.wte = nn.Embedding(config.vocab_size, self.embed_dim)
        self.wpe = nn.Embedding(config.max_position_embeddings, self.embed_dim)
        self.max_position_embeddings = config.max_position_embeddings
        self.drop = nn.Dropout(config.embd_pdrop)
        # self.h = nn.ModuleList(
        #     [PGTBlock(config, layer_idx_1=i, layer_idx_2=i + 1) for i in range(0, config.num_layers * 2, 2)])
        self.h = nn.ModuleList(
            [CC_PGT_Block(config, layer_idx=i) for i in range(config.num_layers)])
        self.ln_f = nn.LayerNorm(self.embed_dim)
        # self.fc = Conv1D(self.embed_dim, config.vocab_size)
        self.fc = nn.Linear(self.embed_dim, config.vocab_size)
        # Model parallel
        self.model_parallel = False
        self.device_map = None
        self.gradient_checkpointing = False
        self.config = config
        self.pad_token_idx = config.pad_token_id
        if apply_init:
            self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)
        elif isinstance(module, Conv1D):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)

    def make_attention_mask(self, inp):
        return inp != self.pad_token_idx

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, new_embeddings):
        self.wte = new_embeddings

    def configure_optimizer(self, config):
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, Conv1D)
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for nm1, p1 in self.named_modules():
            for nm2, p2 in p1.named_parameters():
                fpn = '%s.%s' % (nm1, nm2) if nm1 else nm2

                if nm2.endswith('bias'):
                    no_decay.add(fpn)
                elif nm2.endswith('weight') and isinstance(p1, whitelist_weight_modules):
                    decay.add(fpn)
                elif nm2.endswith('weight') and isinstance(p1, blacklist_weight_modules):
                    no_decay.add(fpn)

        param_dict = {pn: p for pn, p in self.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert len(inter_params) == 0, "parameters %s made it into both decay/no_decay sets!" % (str(inter_params),)
        assert len(
            param_dict.keys() - union_params) == 0, "parameters %s were not separated into either decay/no_decay set!" \
                                                    % (str(param_dict.keys() - union_params),)

        optim_groups = [
            {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": config.weight_decay},
            {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0},
        ]
        optimizer = torch.optim.AdamW(optim_groups, lr=config.lr)
        return optimizer

    def forward(self, inputs: typing.Optional[torch.LongTensor], attention_mask=None, heads_mask=None):
        if self.config.create_attention_mask:
            attention_mask = self.make_attention_mask(inputs)
        token_embeddings = self.wte(inputs)
        pos_embeddings = self.wpe(torch.arange(0, inputs.size(-1), dtype=inputs.dtype, device=inputs.device))
        hidden = self.drop(token_embeddings + pos_embeddings)
        for m in self.h:
            hidden = m(hidden, attention_mask=attention_mask, heads_mask=heads_mask)
        hidden = self.fc(self.ln_f(hidden))
        return hidden

    @torch.no_grad()
    def generate(self, idx, generate=5000, temp=1, eos: int = 102, attention_mask=None):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)
        for _ in range(generate):
            idx = idx[:, -self.max_position_embeddings:]
            pred = self.forward(idx, attention_mask=attention_mask)
            pred = pred[:, -1, :] / temp
            pred = F.softmax(pred, dim=-1)
            next_index = torch.multinomial(pred, 1)
            idx = torch.cat([idx, next_index], 1)
            if next_index[0] == eos:
                break
        return idx

    @torch.no_grad()
    def generate_ca(self, idx, temp=1, attention_mask=None):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)
        idx = idx[:, -self.max_position_embeddings:]
        pred = self.forward(idx, attention_mask=attention_mask)
        pred = pred[:, -1, :] / temp
        pred = F.softmax(pred, dim=-1)
        next_index = torch.multinomial(pred, 1)
        idx = torch.cat([idx, next_index], 1)
        return idx


class PGT_J(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.embed_dim = config.hidden_size

        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)
        elif isinstance(module, Conv1D):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)

    def make_attention_mask(self, inp):
        return inp != self.pad_token_idx

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, new_embeddings):
        self.wte = new_embeddings

    def configure_optimizer(self, config):
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, Conv1D)
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for nm1, p1 in self.named_modules():
            for nm2, p2 in p1.named_parameters():
                fpn = '%s.%s' % (nm1, nm2) if nm1 else nm2

                if nm2.endswith('bias'):
                    no_decay.add(fpn)
                elif nm2.endswith('weight') and isinstance(p1, whitelist_weight_modules):
                    decay.add(fpn)
                elif nm2.endswith('weight') and isinstance(p1, blacklist_weight_modules):
                    no_decay.add(fpn)

        param_dict = {pn: p for pn, p in self.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert len(inter_params) == 0, "parameters %s made it into both decay/no_decay sets!" % (str(inter_params),)
        assert len(
            param_dict.keys() - union_params) == 0, "parameters %s were not separated into either decay/no_decay set!" \
                                                    % (str(param_dict.keys() - union_params),)

        optim_groups = [
            {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": config.weight_decay},
            {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0},
        ]
        optimizer = torch.optim.AdamW(optim_groups, lr=config.lr)
        return optimizer

    def forward(self, inputs: typing.Optional[torch.LongTensor], attention_mask=None, heads_mask=None):
        if self.config.create_attention_mask:
            print('ay you why do you do that ?')
            attention_mask = self.make_attention_mask(inputs)
        token_embeddings = self.wte(inputs)
        pos_embeddings = self.wpe(torch.arange(0, inputs.size(-1), dtype=inputs.dtype, device=inputs.device))
        hidden = self.drop(token_embeddings + pos_embeddings)
        for m in self.h:
            hidden = m(hidden, attention_mask=attention_mask, heads_mask=heads_mask)
        hidden = self.fc(self.ln_f(hidden))
        return hidden

    @torch.no_grad()
    def generate(self, idx, generate=5000, temp=1, eos: int = 2, attention_mask=None):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)
        for _ in range(generate):
            idx = idx[:, -self.max_position_embeddings:]
            pred = self.forward(idx, attention_mask=attention_mask)
            pred = pred[:, -1, :] / temp
            pred = F.softmax(pred, dim=-1)
            next_index = torch.multinomial(pred, 1)
            idx = torch.cat([idx, next_index], 1)
            if next_index[0] == eos:
                break
        return idx

    @torch.no_grad()
    def generate_ca(self, idx, temp=1, attention_mask=None):
        if len(idx.shape) == 1:
            idx = idx.unsqueeze(0)
        idx = idx[:, -self.max_position_embeddings:]
        pred = self.forward(idx, attention_mask=attention_mask)
        pred = pred[:, -1, :] / temp
        pred = F.softmax(pred, dim=-1)
        next_index = torch.multinomial(pred, 1)
        idx = torch.cat([idx, next_index], 1)
        return idx


class LLmP(nn.Module):
    def __init__(self, config: LLmPConfig):
        super(LLmP, self).__init__()
        self.wte = nn.Embedding(config.vocab_size, config.hidden_size)
        self.wte_ln = PMSNorm(config)
        self.h = nn.ModuleList([LLmPBlock(config=config, layer_index=i) for i in range(config.n_layers)])
        self.ln = PMSNorm(config)
        self.dtype = config.dtype
        self.out = nn.Linear(config.hidden_size, config.vocab_size, bias=False, dtype=config.dtype)
        # self.freq = precompute_frq_cis(config.hidden_size // config.n_heads, config.max_sentence_length * 2).to(
        #     self.dtype)
        # i dont use freq or rotaty embedding in LLmP anymore
        self.config = config
        self.apply(self._init_weights)

    @staticmethod
    def _init_weights(module: nn.Module):
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=0.002)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=0.002)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def forward(self, input_ids: Optional[torch.Tensor], attention_mask: Optional[torch.Tensor],
                labels: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, Union[torch.Tensor, None]]:
        batch, seq_len = input_ids.shape
        if attention_mask is not None:
            attention_mask = attention_mask.to(input_ids.device, dtype=self.dtype)
            attention_mask = (1.0 - attention_mask) * torch.finfo(self.dtype).min
            if attention_mask.ndim == 3:
                attention_mask = attention_mask[:, None, :, :]
            if attention_mask.ndim == 2:
                attention_mask = attention_mask[:, None, None, :]
        else:
            attention_mask = torch.ones(input_ids.shape).to(input_ids.device, dtype=self.dtype)
            attention_mask = (1.0 - attention_mask) * torch.finfo(self.dtype).min
            if attention_mask.ndim == 3:
                attention_mask = attention_mask[:, None, :, :]
            if attention_mask.ndim == 2:
                attention_mask = attention_mask[:, None, None, :]
        logger.debug(
            f'We Got INPUT ---**--- :  [ input _ids : {input_ids.shape}] [ attention _mask : {attention_mask.shape if attention_mask is not None else None} ]')
        # self.freq = self.freq.to(input_ids.device)
        # chosen_freq = self.freq[:seq_len]
        # logger.debug(f'chosen_freq : {chosen_freq.shape}')
        alibi = build_alibi_tensor(attention_mask=attention_mask.view(attention_mask.size()[0], -1), dtype=self.dtype,
                                   number_of_heads=self.config.n_heads)

        x = self.wte_ln(self.wte(input_ids))
        logger.debug(f'word tokenizing shape ==> : {x.shape}')
        for i, h in enumerate(self.h):
            logger.debug(f'At Block Index  : \033[32m{i}\033[92m')
            x = h(x, attention_mask=attention_mask, alibi=alibi)
        logits = self.out(self.ln(x))
        loss = None
        if labels is not None:
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            loss = nn.functional.cross_entropy(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        return logits, loss

    def generate(
            self,
            tokens: Optional[torch.Tensor],
            eos_id: int,
            pad_id: int,
            attention_mask=None,
            max_gen_len: int = 20,
            temperature: float = 0.9,
            top_p: float = 0.95,
    ) -> Iterable[torch.Tensor]:
        def sample_top_p(probs, p):
            probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
            probs_sum = torch.cumsum(probs_sort, dim=-1)
            mask = probs_sum - probs_sort > p
            probs_sort[mask] = 0.0
            probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))

            _next_token = torch.multinomial(probs_sort, num_samples=1)

            _next_token = torch.gather(probs_idx, -1, _next_token)
            return _next_token

        if attention_mask is True:
            attention_mask = torch.nn.functional.pad((tokens != 0).float(),
                                                     (0, self.config.max_sentence_length - tokens.size(-1)),
                                                     value=pad_id)
        # attention_mask = None
        for i in range(max_gen_len):
            tokens = tokens[:, -self.config.max_sentence_length:]
            logits, _ = self.forward(tokens, attention_mask)
            logits = logits[:, -1, :]
            if temperature > 0:
                probs = torch.softmax(logits / temperature, dim=-1)
                next_token = sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits, dim=-1)

            next_token = next_token.reshape(*tokens.shape[:-1], 1)
            tokens = torch.cat([tokens, next_token], dim=1)
            if next_token.view(-1)[0] != eos_id:

                yield next_token.view(1, -1)
            else:
                break
