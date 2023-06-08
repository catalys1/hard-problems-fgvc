'''
'''
from typing import Any, Dict, List, Optional, Union

import pytorch_lightning as pl
from pytorch_lightning.utilities.types import STEP_OUTPUT
import timm
import torch
import torchmetrics

from . import objectives


def get_backbone(name, pretrained=True, **kwargs):
    '''Create a model from the timm library.
    '''
    model = timm.create_model(name, pretrained, **kwargs)
    return model


def get_pretrained_submodules(model, prefix=''):
    '''Given a model with pretrained weights, returns a list of submodules that contain
    pretrained parameters; this is likely everything except the final classification
    layer.
    '''
    # this assumes that the only untrained parameters are in a module named "head"
    submods = [''.join((prefix, name)) for name, _ in model.named_children() if name != 'head']
    return submods


def make_parameter_groups(
    model: torch.nn.Module,
    base_lr: float,
    finetune_lr_scale: float=0.1,
    weight_decay: float=0.0,
    finetune_list: Optional[List[str]]=None,
):
    '''Divide the network parameters into groups with different hyperparameters.
    Pretrained weights will get a lower learning rate. Bias parameters and parameters in Normalization
    layers won't have weight decay applied to them.
    '''
    finetune_list = set(finetune_list) or []

    assignments = []

    # initialize parameter groups for scratch and finetune parameters, with or without weight decay
    param_groups = {}
    for key in ('scratch', 'finetune'):
        lr = base_lr * (1 if key == 'scratch' else finetune_lr_scale)
        param_groups[key] = {
            'decay': {'params': [], 'weight_decay': weight_decay, 'lr': lr},
            'no_decay': {'params': [], 'weight_decay': 0.0, 'lr': lr},
        }

    def _divy(name: str, module: torch.nn.Module, finetune: bool=False):
        '''Recursively traverse modules and add parameters to param_groups. Doing it this
        way allows the finetune flag to be inherited by all child modules, and also allows
        for checking whether each module is a normalization layer.
        '''
        finetune = finetune or name in finetune_list
        key1 = 'finetune' if finetune else 'scratch'
        norm = 'Norm' in module.__class__.__name__
        # add module's direct parameters
        for pname, param in module.named_parameters(recurse=False):
            key2 = 'decay'
            if norm or pname.endswith('.bias'):
                key2 = 'no_decay'
            param_groups[key1][key2]['params'].append(param)
            assignments.append(('.'.join((name, pname)), key1, key2))
        # recurse to child modules, inheriting finetuning flag
        for n, m in module.named_children():
            _divy('.'.join((name, n)), m, finetune)

    for name, module in model.named_children():
        _divy(name, module)

    # flatten into a list of param_group dicts
    return (
        [v for g1 in param_groups.values() for v in g1.values()],
        assignments,
    )


def get_optimizer(optim_name):
    return getattr(torch.optim, optim_name)


def get_gpu_memory_usage():
    avail, total = torch.cuda.mem_get_info()
    mem_used = 100 * (1 - (avail / total))
    return f'GPU memory used: {(total - avail) / 1024**3:.2f} of {total / 1024**3:.2f} GB ({mem_used:.2f}%)'


class BaseConfig:
    '''BaseModule configuration.

    Args:
        optimizer_name (str): name of the optimizer.
        base_lr (float): base value of learning rate, before any scaling.
        lr_scale (float): amount of scaling applied to base_lr; this could come, for exmple,
            by applying the linear scaling rule based on batch size.
        finetune_lr_scale (float): amount of scaling applied to the learning rate for layers
            that are being finetuned; typically pretrained layers are finetuned with a lower
            learning rate.
        weight_decay (float): amount of weight decay.
        warmup (float): percentage of training steps during which the learning rate is warmed up
            to its max value.
        optim_kw (Optional[Dict]): additional keyword arguments passed to the optimizer.
    '''
    def __init__(
        self,
        optimizer_name: str='AdamW',
        base_lr: float=1e-3,
        lr_scale: float=1.0,
        finetune_lr_scale: float=0.1,
        weight_decay: float=0.0,
        warmup: float=0.0,
        optim_kw: Optional[Dict]=None,
    ):
        self.optimizer_name = optimizer_name
        self.optim_kw = optim_kw or {}

        self.weight_decay = weight_decay
        self.warmup = warmup

        self.base_lr = base_lr
        self.lr_scale = lr_scale
        self.finetune_lr_scale = finetune_lr_scale


class BaseModule(pl.LightningModule):
    '''BaseModule. Inherits from LightningModule. Base class for defining new Methods.
    Handles optimization setup and hyperparameters.

    Args:
        base_conf (optional dict): a dictionary containing arguments to override BaseConfig defaults.
            See BaseConfig for accepted arguments.
    '''
    def __init__(
        self,
        base_config: Optional[dict]=None,
    ):
        super().__init__()

        self.base_conf = BaseConfig(**(base_config or {}))

        # scaling could come from linear scaling rule based on batch size
        self.lr = self.base_conf.base_lr * self.base_conf.lr_scale

    def on_train_batch_end(self, outputs, batch, batch_idx):
        if self.global_step == 1:
            self.print(get_gpu_memory_usage())

    def configure_optimizers(self) -> Any:
        finetune_lr_scale, weight_decay = self.base_conf.finetune_lr_scale, self.base_conf.weight_decay
        finetuning = getattr(self, 'finetune_list', list())

        # create optimizer
        param_groups, assignments = make_parameter_groups(self, self.lr, finetune_lr_scale, weight_decay, finetuning)
        optimizer = get_optimizer(self.base_conf.optimizer_name)(param_groups, lr=self.lr, **self.base_conf.optim_kw)

        if self.trainer.is_global_zero:
            w = max(len(x[0]) for x in assignments)
            tmp = '{{: <{}}}  {{: <{}}}  {{}}'.format(w, 8)
            print('\n'.join(tmp.format(*x) for x in assignments))

        # create learning rate schedule
        scheduler = {
            'scheduler': torch.optim.lr_scheduler.OneCycleLR(
                optimizer=optimizer,
                max_lr=[g['lr'] for g in param_groups],
                total_steps=self.trainer.estimated_stepping_batches,
                pct_start=self.base_conf.warmup,
            ),
            'interval': 'step',
        }

        return {'optimizer': optimizer, 'lr_scheduler': scheduler}


class ModelConfig:
    '''Basic backbone model configuration.

    Args:
        model_name (str): name of timm model that will be used to create the backbone.
        num_classes (int): number of classes that will be predicted.
        pretrained (str | bool): if bool, use pretrained weights from timm. If str, should be a path
            to a model checkpoint with pretrained weights (default: True).
        model_kw (optional dict): additional keyword arguments passed to timm.create_model.
    '''
    def __init__(
        self,
        model_name: str,
        num_classes: int,
        pretrained: Union[str, bool]=True,
        model_kw: Optional[Dict]=None,
    ):
        self.model_name = model_name
        self.num_classes = num_classes
        self.pretrained = pretrained
        self.model_kw = model_kw or {}


class ImageClassifier(BaseModule):
    '''ImageClassifier.

    Args:
        model_conf (optional dict): a dictionary containing arguments to override ModelConfig defaults.
            See ModelConfig for accepted arguments.
        base_conf (optional dict): a dictionary containing arguments to override BaseConfig defaults.
            See BaseConfig for accepted arguments.
    '''
    def __init__(
        self,
        model_conf: Optional[dict]=None,
        base_conf: Optional[dict]=None,
    ):
        BaseModule.__init__(self, base_conf)
        self.model_conf = ModelConfig(**(model_conf or {}))
        self.num_classes = self.model_conf.num_classes

        # setup the backbone model
        self.inject_backbone_args()
        self.setup_backbone()

        # setup for finetuning
        if self.model_conf.pretrained:
            self.finetune_list = get_pretrained_submodules(self.backbone, prefix='backbone.')

        # setup any additional model components
        self.setup_model()

        # setup loss function and metrics
        self.setup_objective()
        self.setup_metrics()

    def inject_backbone_args(self):
        '''Add method-specific settings to the model config before creating the backbone.'''
        pass

    def setup_backbone(self):
        '''Create the backbone model.'''
        conf = self.model_conf
        conf.model_kw['num_classes'] = self.num_classes
        pt = conf.pretrained if isinstance(conf.pretrained, bool) else False
        self.backbone = get_backbone(conf.model_name, pt, **conf.model_kw)
        if isinstance(conf.pretrained, str):
            # load weights from checkpoint file 
            state = torch.load(conf.pretrained, map_location='cpu')['state_dict']
            model_state = self.backbone.state_dict()
            mismatch = [k for k in state if k in model_state and state[k].shape != model_state[k].shape]
            self.backbone.load_state_dict({k: v for k, v in state.items() if k not in mismatch}, strict=False)
            print(f'Skipping mismatched parameters: {mismatch}')

    def setup_model(self):
        '''Create any additional model components beyond the backbone.'''
        pass

    def setup_objective(self):
        '''Create the objective function.'''
        self.objective = objectives.CrossEntropyLoss()

    def setup_metrics(self):
        '''Create any metrics for logging.'''
        self.train_accuracy = torchmetrics.Accuracy('multiclass', num_classes=self.num_classes)
        self.val_accuracy = torchmetrics.Accuracy('multiclass', num_classes=self.num_classes)

    def forward(self, x):
        return self.backbone(x)

    def step(self, batch, accuracy_metric):
        '''Things that should happen during both training and validation steps.'''
        x, y = batch
        pred = self(x)
        loss = self.objective(pred, y)
        accuracy = accuracy_metric(pred, y)
        return pred, loss, accuracy
    
    def training_step(self, batch, batch_idx) -> STEP_OUTPUT:
        pred, loss, accuracy = self.step(batch, self.train_accuracy)

        self.log('train/loss', loss, prog_bar=True, sync_dist=True)
        self.log('train/acc', accuracy, prog_bar=True, sync_dist=True)

        return {'loss': loss, 'pred': pred}
    
    def validation_step(self, batch, batch_idx) -> STEP_OUTPUT:
        pred, loss, accuracy = self.step(batch, self.val_accuracy)

        self.log('val/loss', loss, prog_bar=True, sync_dist=True)
        self.log('val/acc', accuracy, prog_bar=True, sync_dist=True)

        return {'loss': loss, 'pred': pred}