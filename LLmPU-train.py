import logging
import math
from typing import Tuple, Optional, Union

import erutils
import pandas as pd
import torch
from erutils.loggers import show_hyper_parameters
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from transformers import T5Tokenizer, AutoTokenizer

from config.config import TQDM_KWARGS
from config.llmpu_configs import LLmPU_M
from modules.dataset import DatasetLLmPU
from modules.modeling_llmpu import LLmPUForConditionalGeneration, LLmPUConfig
from utils.utils import make2d, count_model_parameters, save_checkpoints

logging.basicConfig(level=logging.WARN)


def prepare_data(source_mask: Optional[torch.Tensor], source_ids: Optional[torch.Tensor],
                 target_ids: Optional[torch.Tensor], device: Union[torch.device, str]) -> Tuple[
    Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]
]:
    source_mask = make2d(source_mask)
    source_ids = make2d(source_ids)
    target_ids = make2d(target_ids)
    y = target_ids.to(device, dtype=torch.long)
    decoder_input: Optional[torch.Tensor] = y[:, :-1].contiguous()
    lm_labels: Optional[torch.Tensor] = y[:, 1:].clone().detach()
    lm_labels[y[:, 1:] == 0] = -100
    input_id: Optional[torch.Tensor] = source_ids.to(device, dtype=torch.long)
    mask: Optional[torch.Tensor] = source_mask.to(device, dtype=torch.long)
    return input_id, mask, decoder_input, lm_labels


def train(m: Optional[LLmPUForConditionalGeneration],
          optim: Optional[torch.optim.AdamW],
          source_mask: Optional[torch.Tensor],
          source_ids: Optional[torch.Tensor],
          target_ids: Optional[torch.Tensor],
          device: Union[torch.device, str]) -> Optional[torch.Tensor]:
    input_ids, mask, decoder_input, labels = prepare_data(source_mask, source_ids, target_ids, device=device)
    out = m(input_ids=input_ids, attention_mask=mask, decoder_input_ids=decoder_input, labels=labels)
    loss_model = out[0]
    optim.zero_grad()
    loss_model.backward()
    optim.step()
    return loss_model


def _main():
    batch_size = 1
    epochs = 100
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    tokenizer: T5Tokenizer = AutoTokenizer.from_pretrained('tokenizer_model/LLmPU')
    data_frame = pd.read_csv('ipynb/news_summary.csv')
    data_frame["text"] = "summarize: " + data_frame["text"]

    config = LLmPUConfig(vocab_size=tokenizer.vocab_size, **LLmPU_M)
    show_hyper_parameters(config)
    model = LLmPUForConditionalGeneration(config=config).to(device)
    erutils.fprint(f'Model Created with {count_model_parameters(model)} Million Parameters')
    optimizer = torch.optim.AdamW(model.parameters(), 3e-4)
    source_length = config.max_length
    target_length = config.max_length

    dataset = DatasetLLmPU(tokenizer=tokenizer, source_len=source_length, target_len=target_length,
                           source_text=data_frame['text'], target_text=data_frame['headlines'])
    dataloader_kw = dict(batch_size=batch_size, shuffle=True, pin_memory=True)
    dataloader = DataLoader(dataset, **dataloader_kw)

    for epoch in range(epochs):
        total_loss = 0
        with tqdm(iterable=enumerate(dataloader),
                  total=math.ceil(dataset.__len__() / batch_size),
                  **TQDM_KWARGS) as progress_bar:
            for i, data in progress_bar:
                iter_at = (epoch + 1) * i
                _source_ids, _source_mask, _target_ids = data['source_ids'], data['source_mask'], data['target_ids']
                loss = train(model, optimizer, source_mask=_source_mask, source_ids=_source_ids, target_ids=_target_ids,
                             device=device)
                total_loss += loss
                progress_bar.set_postfix(loss=loss.item(), epoch=f'[{epoch}/{epochs}]',
                                         avg=total_loss.item() / (iter_at + 1))
            progress_bar.write('=> Saving Model Checkpoints')
            save_checkpoints(model=model.state_dict(), optimizer=optimizer.state_dict(),
                             epochs=epochs,
                             epoch=epoch + 1,
                             conf=dict(
                                 vocab_size=tokenizer.vocab_size, **LLmPU_M
                             ),
                             name='LLmPU-model.pt')


if __name__ == "__main__":
    _main()