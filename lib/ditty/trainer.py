from dataclasses import dataclass, field
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from accelerate import Accelerator
from accelerate.utils import set_seed, ProjectConfiguration
from transformers.trainer_pt_utils import (
    get_model_param_count,
)
import atexit

import numpy as np
import random

from peft import PeftModelForCausalLM
from logging import getLogger
from typing import Optional
import os

from transformers import (
    PreTrainedModel
)

def default_scheduler_factory(optimizer):
    return torch.optim.lr_scheduler.StepLR(optimizer, step_size=30, gamma=0.1)

logger = getLogger()

@dataclass(kw_only=True)
class TrainerState():
    epoch: int = 0
    steps: int = 0
    global_loss: int = 0

    def state_dict(self):
        return {
            "epoch": self.epoch,
            "steps": self.steps,
            "global_loss": self.global_loss,
        }
    
    def load_state_dict(self, state_dict):
        self.epoch = state_dict["epoch"]
        self.steps = state_dict["steps"]
        self.global_loss = state_dict["global_loss"]


@dataclass(kw_only=True)
class Trainer():
    model: nn.Module
    optimizer: torch.optim.Optimizer
    dataset: DataLoader
    device: torch.device
    scheduler: torch.optim.lr_scheduler._LRScheduler | None = None
    use_scheduler: bool = True
    grad_accum: int = 1
    accelerator_kwargs: dict = field(default_factory=dict)
    fp16: bool = False
    output_dir: str = "./output"
    checkpoint_every: int = 1000
    load_checkpoint: bool = False
    seed: Optional[int] = None

    def __post_init__(self):
        if self.seed:
            set_seed(self.seed)

        os.makedirs(self.output_dir, exist_ok=True)

        self.batch_size = self.dataset.batch_size

        if self.use_scheduler and not self.scheduler:
            self.scheduler = default_scheduler_factory(self.optimizer)

        acc_kwargs = {
            "gradient_accumulation_steps": self.grad_accum,
            "project_dir": self.output_dir,
            "project_config": ProjectConfiguration(
                project_dir=self.output_dir,
                automatic_checkpoint_naming=True,
            )
        }

        acc_kwargs = {**acc_kwargs, **self.accelerator_kwargs}

        self.accelerator = Accelerator(**acc_kwargs)
        device = self.accelerator.device
        self.device = device

        if self.use_scheduler:
            self.model, self.optimizer, self.dataset, self.scheduler = self.accelerator.prepare(self.model, self.optimizer, self.dataset, self.scheduler)

            self.accelerator.register_for_checkpointing(self.scheduler)
        else:
            self.model, self.optimizer, self.dataset = self.accelerator.prepare(self.model, self.optimizer, self.dataset)

        self.state = TrainerState()
        self.accelerator.register_for_checkpointing(self.state)

    def _save_dist(self):
        model = self.accelerator.unwrap_model(self.model)
        model_state = model.state_dict()
        model.save_pretrained(f"{self.output_dir}/dist", state_dict=model_state)

    def _save(self, no_dist=False):
        self.accelerator.wait_for_everyone() 
        self.accelerator.save_state()
        if not no_dist:
            self._save_dist()

    def _train_accelerate(self, epochs=1, max_steps=None):
        self.model.train()

        if self.load_checkpoint:
            try:
                last_cp = sorted(os.listdir(f"{self.output_dir}/checkpoints/"))[-1]
                logger.info(f"Trying to load checkpoint: {last_cp}....")
                self.accelerator.load_state(f"{self.output_dir}/checkpoints/{last_cp}")

                # Update the iteration number so that the next checkpoint name is increased by 1
                last_cp_num = last_cp.split("_")[-1]
                self.accelerator.project_configuration.iteration = int(last_cp_num) + 1
                logger.info("Checkpoint loaded.")
            except FileNotFoundError as e:
                logger.warning(e)
                logger.warning("No checkpoint found, starting from scratch.")
                self._save(no_dist=True)
        else:
            self._save(no_dist=True)

        atexit.register(self._save)
        for ep in range(self.state.epoch, epochs):
            dataset = self.dataset

            if self.state.steps > 0:
                dataset = self.accelerator.skip_first_batches(self.dataset, self.state.steps)

            for batch_idx, batch in enumerate(dataset):
                with self.accelerator.accumulate(self.model):
                    outputs = self.model(**batch)
                    loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]

                    self.accelerator.backward(loss)

                    self.optimizer.step()
                    if self.use_scheduler:
                        self.scheduler.step()
                    self.optimizer.zero_grad()

                    batch_loss = loss.item() / self.grad_accum

                    print(f"Epoch {ep} | Batch {batch_idx} | Loss {batch_loss}")

                    self.state.global_loss += batch_loss

                self.state.steps += 1
                if max_steps is not None and batch_idx >= max_steps:
                    break

                if batch_idx % self.checkpoint_every == 0:
                    self._save()

            self.state.epoch += 1
        atexit.unregister(self._save)
        self._save()

        return self.state.global_loss / self.state.steps

    def train(self, epochs=1, max_steps=None):
        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {len(self.dataset):,}")
        logger.info(f"  Num Epochs = {epochs:,}")
        if max_steps:
            logger.info(f"  Total optimization steps = {max_steps:,}")
        logger.info(f"  Instantaneous batch size per device = {self.batch_size:,}")
        logger.info(f"  Gradient Accumulation steps = {self.grad_accum}")
        logger.info(f"  Number of trainable parameters = {get_model_param_count(self.model, trainable_only=True):,}")

        return self._train_accelerate(epochs=epochs, max_steps=max_steps)
        