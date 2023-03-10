import argparse
import logging
import math
import os.path
import typing
from typing import Optional, Union, Tuple

import erutils
import torch.utils.data
from datasets import load_dataset
from erutils.loggers import fprint
from torch import Tensor
from torch.utils.tensorboard import SummaryWriter
from tqdm.auto import tqdm
from transformers import GPT2Tokenizer, AutoTokenizer

from config.config import TQDM_KWARGS
from modules.dataset import DatasetLLMoU
from modules.modeling_LLMoU import LLMoUModel, LLMoUConfig
from utils.utils import make2d, save_checkpoints, get_config_by_name, device_info, get_memory, count_model_parameters, \
    create_output_path

# torch.manual_seed(42)
torch.backends.cudnn.benchmark = True

pars = argparse.ArgumentParser()

pars.add_argument('--batch', '--batch', type=int, default=1)
pars.add_argument('--train', '--train', type=bool, default=True)
pars.add_argument('--compile', '--compile', type=bool, default=True)
pars.add_argument('--weight', '--weight', type=str, default='out/LLMoU-ML/weights/LLMoU-ML-model.pt')
pars.add_argument('--out-path', '--out-path', type=str, default='out')
pars.add_argument('--model', '--model', type=str, default='LLMoU-ML')
pars.add_argument('--data-src', '--data-src', type=str, default='HF-super_glue/multirc')

options = pars.parse_args()

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.CRITICAL)


def inter_q(question: Optional[str], tokenizer: GPT2Tokenizer) \
        -> Tuple[torch.Tensor, torch.Tensor]:
    out = tokenizer.encode_plus(question, return_tensors='pt')
    return out['input_ids'], out['attention_mask']


def train(input_ids: Optional[Tensor],
          targets: Optional[Tensor],
          attention_mask: Optional[Tensor],
          network: Optional[LLMoUModel.forward],
          optim: Optional[torch.optim.AdamW],
          loss_average: Optional[Tensor],
          device: Union[torch.device, str]) -> [typing.Union[torch.Tensor],
                                                typing.Union[torch.Tensor]]:
    labels: Optional[Tensor] = make2d(targets.type(torch.long).to(device))
    input_ids: Optional[Tensor] = make2d(input_ids.type(torch.long).to(device))
    attention_mask: Optional[Tensor] = make2d(attention_mask.type(torch.long).to(device))
    logger.debug('RUNNING TRAIN FUNCTION IN MAIN THREAD ')
    _, loss = network(input_ids=input_ids, labels=labels, attention_mask=attention_mask)

    loss_average += loss.item()
    optim.zero_grad(set_to_none=True)
    loss.backward()
    optim.step()
    return loss, loss_average


def main(opt):
    if opt.weight is None:
        out_path = create_output_path(path=opt.out_path, name=opt.model)
        if not os.path.exists(os.path.join(out_path, 'weights')):
            os.mkdir(os.path.join(out_path, 'weights'))
    else:
        if opt.weight.endswith('.pt'):
            out_path = opt.weight.split('/')
            if 'weights' in out_path:
                out_path = os.path.join(*(p for p in out_path[:-2]))
            else:
                out_path = os.path.join(*(p for p in out_path[:-1]))
        else:
            raise ValueError('weight must contain path to .pt file')
    device_info()
    erutils.fprint(f'OUT DIR : {out_path}')
    if opt.data_src.endswith('.txt'):
        data = open(opt.data_src, 'r', encoding='utf8').read().split()
    elif opt.data_src.endswith('.json'):
        data = opt.data_src
    elif opt.data_src.startswith('HF-'):
        name = opt.data_src.replace('HF-', '')
        if '/' in name:
            model_name = name.split('/')
            data = load_dataset(model_name[0], model_name[1])
        else:
            data = load_dataset(name)
    else:
        data = None
        raise ValueError()
    parameters: LLMoUConfig = get_config_by_name(opt.model)
    tokenizer: GPT2Tokenizer = AutoTokenizer.from_pretrained('tokenizer_model/LLMoU-C')

    dataset = DatasetLLMoU(data=data, max_length=parameters.max_sentence_length, tokenizer=tokenizer)
    parameters.vocab_size = dataset.tokenizer.vocab_size

    parameters.vocab_size += 7
    # parameters.device = 'cpu'
    parameters.data_path = opt.data_src

    parameters.batch_size = opt.batch
    dataloader = torch.utils.data.DataLoader(dataset=dataset, batch_size=parameters.batch_size, num_workers=4,
                                             pin_memory=True)
    erutils.loggers.show_hyper_parameters(parameters)

    fprint('Loading Model ...' if opt.weight is not None else 'Creating Model ...')

    model = LLMoUModel(config=parameters).to(parameters.device) if opt.weight is not None else LLMoUModel(
        config=parameters).to('cpu')
    optimizer_kwargs = dict(lr=parameters.lr, weight_decay=parameters.weight_decay)
    optimizer = torch.optim.AdamW(model.parameters(), **optimizer_kwargs)
    model_parameters_size: typing.Optional[float] = count_model_parameters(model)

    checkpoints = torch.load(opt.weight, 'cpu') if opt.weight is not None else None

    if checkpoints is not None:
        model.load_state_dict(checkpoints['model'])
        model = model.to(parameters.device)
        optimizer.load_state_dict(checkpoints['optimizer'])
    fprint(
        f'Model Loaded With {model_parameters_size} Million Parameters' if opt.weight is not None
        else f'Model Created With {model_parameters_size} Million Parameters')

    if opt.compile:
        model = torch.compile(model)
        fprint(f"Model Compiled Successfully")
    board = SummaryWriter(log_dir=f'{out_path}/tensorboard', filename_suffix=f'{opt.model}')
    at = 0 if opt.weight is None else checkpoints['at']

    question = 'paragraph: my name is erfan question: what is my name ?' + dataset.agent
    model = model.to(device=parameters.device)

    if opt.train:
        logger.info('TRAIN IS ABOUT TO START')
        for epoch in range(checkpoints['epoch'] if opt.weight is not None else 0, parameters.epochs):
            loss_avg = 0
            with tqdm(enumerate(dataloader), **TQDM_KWARGS,
                      total=math.ceil(dataset.__len__() // parameters.batch_size)) as progress_bar:
                for i, (input_ids_t, attention_mask) in progress_bar:
                    logger.debug(f'\033[1;94m input_ids_t    : {input_ids_t.shape}')
                    logger.debug(f'\033[1;94m attention_mask : {attention_mask.shape}')

                    loss, loss_avg = train(input_ids=input_ids_t, targets=input_ids_t, network=model,
                                           optim=optimizer,
                                           loss_average=loss_avg, device=parameters.device,
                                           attention_mask=attention_mask)

                    free_gpu, used_gpu, total_gpu = get_memory(0)
                    if ((i + 1) % 50) == 0:
                        tk, _ = inter_q(question, tokenizer=tokenizer
                                        )
                        tk = tk.to(parameters.device)
                        cals = []
                        try:
                            for pred in model.generate(tokens=tk,
                                                       attention_mask=None,
                                                       eos_id=tokenizer.eos_token_id):
                                cals.append(pred)
                            cals = torch.cat(cals, dim=-1)
                            cals = cals.to('cpu')
                            awn = tokenizer.decode(cals[0])
                        except:
                            awn = 'error'
                            pass
                        del cals

                        board.add_scalar('train/Loss', scalar_value=loss.item(), global_step=at)
                        board.add_scalar('train/avg-Loss', scalar_value=(loss_avg / (i + 1)),
                                         global_step=at)
                        board.add_text('train/Context', f'{question}', global_step=at)
                        board.add_text('train/GeneratedResponse', f'{awn}', global_step=at)
                    at += 1
                    progress_bar.set_postfix(epoch=f'[{epoch}/{parameters.epochs}]', device=parameters.device,
                                             loss_avg=(loss_avg / (i + 1)),
                                             loss=loss.item(), free_GPU=free_gpu, used_GPU=used_gpu)

                print()
                save_checkpoints(model=model.state_dict(), optimizer=optimizer.state_dict(),
                                 epochs=parameters.epochs, at=at,
                                 epoch=epoch + 1, config=opt.model,
                                 name=f'{out_path}/weights/{opt.model}-model.pt')
                progress_bar.write('==> MODEL SAVED SUCCESSFULLY')


if __name__ == "__main__":
    main(options)
