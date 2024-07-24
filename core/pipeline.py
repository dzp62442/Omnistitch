import os
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
import itertools
import argparse

from importlib import import_module
from torch.optim import AdamW
from torch.nn.parallel import DistributedDataParallel as DDP

from core.loss import EPE, Ternary, VGGPerceptualLoss

from core.model.vsla_model import Model as vsla_model
from core.model.omnistitch import Model as omnistitch

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class Pipeline:
    def __init__(self,
            model_cfg_dict,
            optimizer_cfg_dict=None,
            local_rank=-1,
            training=False,
            resume=False
            ):
        self.model_cfg_dict = model_cfg_dict
        self.optimizer_cfg_dict = optimizer_cfg_dict
        self.epe = EPE()
        self.ter = Ternary()
        self.vgg = VGGPerceptualLoss()

        self.init_model()
        self.device()
        self.training = training

        # We note that in practical, the `lr` of AdamW is reset from the
        # outside, using cosine annealing during the while training process.
        if training:
            self.optimG = AdamW(itertools.chain(
                filter(lambda p: p.requires_grad, self.model.parameters())),
                lr=optimizer_cfg_dict["init_lr"],
                weight_decay=optimizer_cfg_dict["weight_decay"])

        # `local_rank == -1` is used for testing, which does not need DDP
        if local_rank != -1:
            self.model = DDP(self.model, device_ids=[local_rank],
                    output_device=local_rank, find_unused_parameters=False)

        # Restart the experiment from last saved model, by loading the state of
        # the optimizer
        if resume:
            assert training, "To restart the training, please init the"\
                    "pipeline with training mode!"
            print("Load optimizer state to restart the experiment")
            ckpt_dict = torch.load(optimizer_cfg_dict["ckpt_file"])
            self.optimG.load_state_dict(ckpt_dict["optimizer"])


    def train(self):
        self.model.train()


    def eval(self):
        self.model.eval()


    def device(self):
        self.model.to(DEVICE)


    @staticmethod
    def convert_state_dict(rand_state_dict, pretrained_state_dict):
        param =  {
            k.replace("module.", "", 1): v
            for k, v in pretrained_state_dict.items()
            }
        param = {k: v
                for k, v in param.items()
                if ((k in rand_state_dict) and (rand_state_dict[k].shape \
                        == param[k].shape))
                }
        rand_state_dict.update(param)
        return rand_state_dict


    def init_model(self):

        def load_pretrained_state_dict(model, model_file):
            if (model_file == "") or (not os.path.exists(model_file)):
                raise ValueError(
                        "Please set the correct path for pretrained model!")

            print("Load pretrained model from %s."  % model_file)
            rand_state_dict = model.state_dict()
            pretrained_state_dict = torch.load(model_file)

            return Pipeline.convert_state_dict(
                    rand_state_dict, pretrained_state_dict)

        # check args
        model_cfg_dict = self.model_cfg_dict
        model_name = model_cfg_dict["model_name"] \
                if "model_name" in model_cfg_dict else "omnistitch"
        pyr_level = model_cfg_dict["pyr_level"] \
                if "pyr_level" in model_cfg_dict else 4
        nr_lvl_skipped = model_cfg_dict["nr_lvl_skipped"] \
                if "nr_lvl_skipped" in model_cfg_dict else 1
        load_pretrain = model_cfg_dict["load_pretrain"] \
                if "load_pretrain" in model_cfg_dict else True
        model_file = model_cfg_dict["model_file"] \
                if "model_file" in model_cfg_dict else ""
                
        # instantiate model
        if model_name == "omnistitch":
            self.model = omnistitch(pyr_level, nr_lvl_skipped)
        # elif model_name == "vsla_like":
        #     self.model = vsla_like()
        else:
            print(f"Please check model name")
            
        # load pretrained model weight
        if load_pretrain:
            state_dict = load_pretrained_state_dict(
                    self.model, model_file)
            self.model.load_state_dict(state_dict)
        else:
            print("Train from random initialization.")


    def save_optimizer_state(self, path, rank, step, best=False):
        if rank == 0:
            optimizer_ckpt = {
                     "optimizer": self.optimG.state_dict(),
                     "step": step
                     }
            torch.save(optimizer_ckpt, "{}/optimizer-ckpt.pth".format(path))
            if best:
                torch.save(optimizer_ckpt, "{}/best-optimizer-ckpt.pth".format(path))
                
            


    def save_model(self, path, rank, save_step=None, best=False):
        if (rank == 0) and (save_step is None):
            torch.save(self.model.state_dict(), '{}/model.pkl'.format(path))
            if best:
                torch.save(self.model.state_dict(), '{}/best-model.pkl'.format(path))
                
        if (rank == 0) and (save_step is not None):
            torch.save(self.model.state_dict(), '{}/model-{}.pkl'\
                    .format(path, save_step))

    def inference(self, img0, img1,
            pyr_level=None,
            nr_lvl_skipped=None):
        interp_img, bi_flow, _ = self.model(img0, img1,
                pyr_level=pyr_level,
                nr_lvl_skipped=nr_lvl_skipped)
        return interp_img, bi_flow
    
    def inference_test(self, img0, img1,
            pyr_level=None,
            nr_lvl_skipped=None):
        interp_img, bi_flow, extra_dict = self.model(img0, img1,
                pyr_level=pyr_level,
                nr_lvl_skipped=nr_lvl_skipped)
        return interp_img, bi_flow, extra_dict

    def train_one_iter(self, img0, img1, gt, learning_rate=0, 
            loss_type="l2+census+vgg", clip_grad=None):
        for param_group in self.optimG.param_groups:
            param_group['lr'] = learning_rate
        self.train()

        interp_img, bi_flow, info_dict = self.model(img0, img1)

        with torch.no_grad():
            loss_interp_l2_nograd = (((interp_img - gt) ** 2 + 1e-6) ** 0.5)\
                    .mean()
            loss_vgg_nograd = self.vgg(interp_img, gt).mean()
            if loss_type == "l2+census+vgg" or loss_type == "l2+census":
                loss_census_nograd = self.ter(interp_img, gt).mean()

        loss_G = 0
        if loss_type == "l2":
            loss_interp_l2 = (((interp_img - gt) ** 2 + 1e-6) ** 0.5).mean()
            loss_G = loss_interp_l2
        elif loss_type == "l2+census":
            loss_interp_l2 = (((interp_img - gt) ** 2 + 1e-6) ** 0.5).mean()
            loss_ter = self.ter(interp_img, gt).mean()
            loss_G = loss_interp_l2 + loss_ter
        elif loss_type == "l2+vgg":
            loss_interp_l2 = (((interp_img - gt) ** 2 + 1e-6) ** 0.5).mean()
            loss_vgg = self.vgg(interp_img, gt).mean() ## LPVS_vimeo : 2e-2
            loss_G = loss_interp_l2 + (loss_vgg * 1.0e-2)
        elif loss_type == "l2+census+vgg":
            loss_interp_l2 = (((interp_img - gt) ** 2 + 1e-6) ** 0.5).mean()
            loss_ter = self.ter(interp_img, gt).mean()
            loss_vgg = self.vgg(interp_img, gt).mean() * 1.0e-2 ## LPVS_vimeo : 2e-2
            loss_G = loss_interp_l2 + loss_ter + loss_vgg
        else:
            ValueError("unsupported loss type!")


        self.optimG.zero_grad()
        loss_G.backward()
            
        if clip_grad:
            nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=clip_grad)
        self.optimG.step()

        extra_dict = {}
        extra_dict["loss_interp_l2"] = loss_interp_l2_nograd
        extra_dict["loss_vgg"] = loss_vgg_nograd
        extra_dict["bi_flow"] = bi_flow
        if loss_type == "l2+census+vgg" or loss_type == "l2+census+vgg2" or loss_type == "l2+census":
            extra_dict["loss_census"] = loss_census_nograd

        return interp_img, extra_dict



if __name__ == '__main__':
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    pass