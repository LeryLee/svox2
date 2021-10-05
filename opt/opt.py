# First, install svox2
# Then, python opt.py <path_to>/nerf_synthetic/<scene> -t ckpt/<some_name>
# or use launching script:   sh launch.sh <EXP_NAME> <GPU> <DATA_DIR>
import torch
import torch.nn.functional as F
import svox2
import json
import imageio
import os
from os import path
import gc
import numpy as np
import math
import argparse

from torch.utils.tensorboard import SummaryWriter

from tqdm import tqdm
from typing import NamedTuple, Optional, Union

device = "cuda" if torch.cuda.is_available() else "cpu"

parser = argparse.ArgumentParser()
parser.add_argument('data_dir', type=str)
parser.add_argument('--train_dir', '-t', type=str, default='ckpt',
                     help='checkpoint and logging directory')
parser.add_argument('--reso', type=int, default=256, help='grid resolution')
parser.add_argument('--sh_dim', type=int, default=9, help='SH dimensions, must be square number >=1, <= 16')
parser.add_argument('--batch_size', type=int, default=5000, help='batch size')
parser.add_argument('--eval_batch_size', type=int, default=200000, help='evaluation batch size')
parser.add_argument('--lr_sigma', type=float, default=2e7, help='SGD lr for sigma')
parser.add_argument('--lr_sh', type=float, default=2e6, help='SGD lr for SH')
parser.add_argument('--n_epochs', type=int, default=20)
parser.add_argument('--print_every', type=int, default=20, help='print every')

parser.add_argument('--init_rgb', type=float, default=0.0, help='initialization rgb (pre-sigmoid)')
parser.add_argument('--init_sigma', type=float, default=0.1, help='initialization sigma')
parser.add_argument('--no_lerp', action='store_true', default=False,
                    help='use nearest neighbor interp (faster)')
args = parser.parse_args()

os.makedirs(args.train_dir, exist_ok=True)
summary_writer = SummaryWriter(args.train_dir)

class Rays(NamedTuple):
    origins: torch.Tensor
    dirs: torch.Tensor
    gt: torch.Tensor

class Dataset():
    """
    NeRF dataset loader
    """
    focal: float
    c2w: torch.Tensor # (n_images, 4, 4)
    gt: torch.Tensor  # (n_images, h, w, 3)
    h: int
    w: int
    n_images: int
    rays: Optional[Rays]
    split: str

    def __init__(self, root, split,
                 device : Union[str, torch.device]='cpu',
                 scene_scale : float = 1.0/1.5):
        all_c2w = []
        all_gt = []

        data_path = path.join(root, split)
        data_json = path.join(root, 'transforms_' + split + '.json')
        print('LOAD DATA', data_path)
        j = json.load(open(data_json, 'r'))

        for frame in tqdm(j['frames']):
            fpath = path.join(data_path, path.basename(frame['file_path']) + '.png')
            c2w = torch.tensor(frame['transform_matrix'], dtype=torch.float32, device=device)

            im_gt = imageio.imread(fpath).astype(np.float32) / 255.0
            im_gt = im_gt[..., :3] * im_gt[..., 3:] + (1.0 - im_gt[..., 3:])
            all_c2w.append(c2w)
            all_gt.append(torch.from_numpy(im_gt))
        self.focal = float(0.5 * all_gt[0].shape[1] / np.tan(0.5 * j['camera_angle_x']))
        self.c2w = torch.stack(all_c2w).to(device=device)
        self.gt = torch.stack(all_gt)
        self.n_images, self.h, self.w, _ = self.gt.shape
        self.split = split

        # Generate rays
        origins = self.c2w[:, None, :3, 3].expand(-1, self.h * self.w, -1).contiguous()
        yy, xx = torch.meshgrid(
            torch.arange(self.h, dtype=torch.float32, device=device),
            torch.arange(self.w, dtype=torch.float32, device=device),
        )
        xx = (xx - self.w * 0.5) / self.focal
        yy = (yy - self.h * 0.5) / self.focal
        zz = torch.ones_like(xx)
        dirs = torch.stack((xx, -yy, -zz), dim=-1)  # OpenGL convention (NeRF)
        dirs /= torch.norm(dirs, dim=-1, keepdim=True)
        dirs = dirs.reshape(1, -1, 3, 1)
        del xx, yy, zz
        dirs = (self.c2w[:, None, :3, :3] @ dirs)[..., 0]

        gt = self.gt.reshape(self.n_images, -1, 3).to(device=device)
        origins = origins * scene_scale
        if split == 'train':
            origins = origins.view(-1, 3)
            dirs = dirs.view(-1, 3)
            gt = gt.view(-1, 3)

        self.rays = Rays(origins=origins, dirs=dirs, gt=gt)

    def shuffle_rays(self):
        """
        Shuffle all rays
        """
        if self.split == 'train':
            print("Shuffle rays")
            perm = torch.randperm(self.rays.origins.size(0), device=self.rays.origins.device)
            self.rays = Rays(origins = self.rays.origins[perm],
                    dirs = self.rays.dirs[perm],
                    gt = self.rays.gt[perm])

class Timing:
    """
    Timing environment
    usage:
    with Timing("message"):
        your commands here
    will print CUDA runtime in ms
    """
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = torch.cuda.Event(enable_timing=True)
        self.end = torch.cuda.Event(enable_timing=True)
        self.start.record()

    def __exit__(self, type, value, traceback):
        self.end.record()
        torch.cuda.synchronize()
        print(self.name, 'elapsed', self.start.elapsed_time(self.end), 'ms')


torch.manual_seed(20200823)
np.random.seed(20200823)

dset = Dataset(args.data_dir, split="train", device=device)
dset_test = Dataset(args.data_dir, split="test")

grid = svox2.SparseGrid(reso=args.reso,
                        radius=1.0,
                        basis_dim=args.sh_dim,
                        use_z_order=True,
                        device=device)
grid.data.data[..., 1:] = args.init_rgb
grid.data.data[..., :1] = args.init_sigma

grid.requires_grad_(True)
step_size = 0.5  # 0.5 of a voxel!
epoch_size = dset.rays.origins.size(0)
batches_per_epoch = (epoch_size-1)//args.batch_size+1

grid.opt.step_size = step_size
grid.opt.sigma_thresh = 1e-8
grid.opt.linear_interp = not args.no_lerp

for epoch_id in range(args.n_epochs):
    # Test
    def eval_step():
        # Put in a function to avoid memory leak
        print('Eval step')
        with torch.no_grad():
            im_size = dset_test.h * dset_test.w
            stats_test = {'psnr' : 0.0, 'mse' : 0.0}
            N_IMGS_TO_SAVE = 5
            img_save_interval = dset_test.n_images // N_IMGS_TO_SAVE
            gstep_id = epoch_id * batches_per_epoch
            n_images_gen = 0
            for img_id in tqdm(range(0, dset_test.n_images, img_save_interval)):
                all_rgbs = []
                all_mses = []
                for batch_begin in range(0, im_size, args.eval_batch_size):
                    batch_end = min(batch_begin + args.eval_batch_size, im_size)
                    batch_origins = dset_test.rays.origins[img_id][batch_begin: batch_end].to(device=device)
                    batch_dirs = dset_test.rays.dirs[img_id][batch_begin: batch_end].to(device=device)
                    rgb_gt_test = dset_test.rays.gt[img_id][batch_begin: batch_end].to(device=device)

                    rays = svox2.Rays(batch_origins, batch_dirs)
                    rgb_pred_test = grid.volume_render(rays, use_kernel=True)
                    all_rgbs.append(rgb_pred_test.cpu())
                    all_mses.append(((rgb_gt_test - rgb_pred_test) ** 2).cpu())
                if len(all_rgbs):
                    im = torch.cat(all_rgbs).view(dset_test.h, dset_test.w, all_rgbs[0].size(-1))
                    summary_writer.add_image(f'test/image_{img_id:04d}',
                            im, global_step=gstep_id, dataformats='HWC')
                mse_num : float = torch.cat(all_mses).mean().item()
                psnr = -10.0 * math.log10(mse_num)
                stats_test['mse'] += mse_num
                stats_test['psnr'] += psnr
                n_images_gen += 1

            stats_test['mse'] /= n_images_gen
            stats_test['psnr'] /= n_images_gen
            for stat_name in stats_test:
                summary_writer.add_scalar('test/' + stat_name,
                        stats_test[stat_name], global_step=gstep_id)
            summary_writer.add_scalar('epoch_id', float(epoch_id), global_step=gstep_id)
            print('eval stats:', stats_test)
    eval_step()
    gc.collect()

    def train_step():
        print('Train step')
        pbar = tqdm(enumerate(range(0, epoch_size, args.batch_size)), total=batches_per_epoch)
        stats = {"mse" : 0.0, "psnr" : 0.0}
        dset.shuffle_rays()
        for iter_id, batch_begin in pbar:
            batch_end = min(batch_begin + args.batch_size, epoch_size)
            batch_origins = dset.rays.origins[batch_begin: batch_end]
            batch_dirs = dset.rays.dirs[batch_begin: batch_end]
            rgb_gt = dset.rays.gt[batch_begin: batch_end]
            rays = svox2.Rays(batch_origins, batch_dirs)
            rgb_pred = grid.volume_render(rays, use_kernel=True)

            mse = F.mse_loss(rgb_gt, rgb_pred)

            # Stats
            mse_num : float = mse.detach().item()
            psnr = -10.0 * math.log10(mse_num)
            stats['mse'] += mse_num
            stats['psnr'] += psnr
            #  stats['invsqr_mse'] += 1.0 / mse_num ** 2

            if (iter_id + 1) % args.print_every == 0:
                # Print averaged stats
                gstep_id = iter_id + epoch_id * batches_per_epoch
                pbar.set_description(f'epoch {epoch_id}/{args.n_epochs} psnr={psnr:.2f}')
                for stat_name in stats:
                    stat_val = stats[stat_name] / args.print_every
                    summary_writer.add_scalar(stat_name, stat_val, global_step=gstep_id)
                    stats[stat_name] = 0.0

            # Backprop
            mse.backward()

            # Manual SGD step
            grid.data.grad[..., 1:] *= args.lr_sh
            grid.data.grad[..., :1] *= args.lr_sigma
            grid.data.data -= grid.data.grad
            del grid.data.grad  # Save memory

    train_step()
    gc.collect()

    #  ckpt_path = path.join(args.train_dir, f'ckpt_{epoch_id:05d}.npy')
    # Overwrite prev checkpoints since they are very huge
    ckpt_path = path.join(args.train_dir, f'ckpt.npy')
    print('Saving', ckpt_path)
    np.savez(ckpt_path,
             links=grid.links.cpu().numpy(),
             data=grid.data.data.cpu().numpy())

    #  if epoch_id == 0:
    #      print('Upsampling!!!')
    #      grid.resample(reso=512)
