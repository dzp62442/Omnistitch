import os
import sys
import shutil
import math
import numpy as np
import cv2
import argparse
import warnings

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader
import lpips
import random

from core.utils import flow_viz
from core.pipeline import Pipeline
from core.dataset import GV360
from core.utils.pytorch_msssim import ssim_matlab

warnings.filterwarnings("ignore")

def evaluate(ppl, data_root, batch_size, nr_data_worker=1):
        dataset = GV360(data_root=data_root, val=True)
        val_data = DataLoader(dataset, shuffle=False, batch_size=batch_size, num_workers=nr_data_worker, pin_memory=True)
        
        psnr_list = []
        ssim_list = []
        lpips_vgg_list = []
        
        precision = 0
        nr_val = val_data.__len__()
        loss_fn_vgg = lpips.LPIPS(net='vgg').to(DEVICE)
            
        for i, data in enumerate(val_data):
                data_gpu = data[0] if isinstance(data, list) else data
                data_gpu = data_gpu.to(DEVICE, non_blocking=True) / 255.
                
                img0 = data_gpu[:, :3]
                img1 = data_gpu[:, 3:6]
                gt = data_gpu[:, 6:9]
                
                with torch.no_grad():
                        pred, _ = ppl.inference(img0, img1, pyr_level=4, nr_lvl_skipped=1)
                
                batch_psnr = []
                batch_ssim = []
                batch_lpips_vgg = []
                
                for j in range(gt.shape[0]):
                        this_gt = gt[j]
                        this_pred = pred[j]
                        interp_img = (this_pred * 255).byte().cpu().numpy().transpose(1, 2, 0)
                        if args.save_root is not None:
                                cv2.imwrite(os.path.join(SAVE_DIR, f'pred_{precision}.png'), interp_img)
                        ssim = ssim_matlab(
                                this_pred.unsqueeze(0),
                                this_gt.unsqueeze(0)).cpu().numpy()
                        ssim = float(ssim)
                        ssim_list.append(ssim)
                        batch_ssim.append(ssim)
                        psnr = -10 * math.log10(
                                torch.mean(
                                        (this_gt - this_pred) * (this_gt - this_pred)
                                        ).cpu().data)
                        psnr_list.append(psnr)
                        batch_psnr.append(psnr)
                        
                        loss_vgg = loss_fn_vgg(this_gt, this_pred).cpu().numpy()
                        lpips_vgg_list.append(loss_vgg)
                        batch_lpips_vgg.append(loss_vgg)
                        precision += 1
                
                print('batch: {}/{}; psnr: {:.4f}; ssim: {:.4f}; lpips_vgg: {:.4f}'.format(i, nr_val,
                np.mean(batch_psnr), np.mean(batch_ssim), np.mean(batch_lpips_vgg)))
                
        psnr = np.array(psnr_list).mean()
        print('average psnr: {:.4f}'.format(psnr))
        ssim = np.array(ssim_list).mean()
        print('average ssim: {:.4f}'.format(ssim))
        lpips_vgg = np.array(lpips_vgg_list).mean()
        print('average lpips_vgg: {:.4f}'.format(lpips_vgg))

if __name__ == "__main__":
        parser = argparse.ArgumentParser(description='benchmark on GV360 dataset with stitched result')
        #**********************************************************#
        
        # => args for dataset and data loader
        parser.add_argument('--data_root', type=str, default='/home/B_UserData/dongzhipeng/Datasets/GV360/GV360_testset', \
                help='root dir of GV360 testset')
        parser.add_argument('--save_root', type=str, default='./demo/omnistitch/GV360',
                help='root dir of predicted result')
        parser.add_argument('--batch_size', type=int, default=4,
                help='batch size for data loader')
        parser.add_argument('--nr_data_worker', type=int, default=1,
                help='number of the worker for data loader')
        #**********************************************************#
        
        # => args for model
        parser.add_argument('--pyr_level', type=int, default=4,
                help='the number of pyramid levels of Omnistitch in testing')
        parser.add_argument('--model_name', type=str, default="omnistitch",
                help='model name, default is omnistitch')
        parser.add_argument('--model_file', type=str,
                default="./train-log-/Omnistitch/trained-models/model.pkl", # omnistitch
                help='weight of Omnistitch')
        #**********************************************************#
        
        # => init the benchmarking environment
        DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        torch.set_grad_enabled(False)
        if torch.cuda.is_available():
                torch.backends.cudnn.enabled = True
                torch.backends.cudnn.demo = True
        torch.backends.cudnn.benchmark = True
        #**********************************************************#
        
        # => init the pipeline and start to benchmark
        args = parser.parse_args()
        
        SAVE_DIR = args.save_root
        if args.save_root:
                if not os.path.exists(SAVE_DIR):
                        os.makedirs(SAVE_DIR)
                if os.path.exists(SAVE_DIR):
                        shutil.rmtree(SAVE_DIR)
                        os.makedirs(SAVE_DIR)
        
        model_cfg_dict = dict(
                load_pretrain = True,
                model_name = args.model_name,
                model_file = args.model_file,
                pyr_level = args.pyr_level,
                nr_lvl_skipped = args.pyr_level - 3
                )
        ppl = Pipeline(model_cfg_dict)
        
        print("Omnistitch benchmarking on GV360 testset...")
        evaluate(ppl, args.data_root, args.batch_size, args.nr_data_worker)
        print(f"{args.data_root}")
        print(f"{args.model_name}")
    
# CUDA_VISIBLE_DEVICES=1 python3 -m scripts.benchmark_GV360