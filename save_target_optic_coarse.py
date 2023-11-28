# save target data with coarse annotation noise
import numpy as np
import random
import torch
import os
from PIL import Image
import math
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset
from torch.optim.lr_scheduler import CosineAnnealingLR
import cv2
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.data import DataLoader
from torchvision import transforms
import sklearn.metrics as metrics
import argparse
import copy
import utils.log
from evaluate_optic import evaluate_save_optic
from data.dataloader_optic_preprocess import REFUGE_Dataset, REFUGE2_Dataset, ORIGA_Dataset, G1020_Dataset
import pdb

MAX_LOSS = 9 * (10 ** 9)


def str2bool(v):
    """
    Input:
        v - string
    output:
        True/False
    """
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


# ==================
# Argparse
# ==================
parser = argparse.ArgumentParser(description='DA noise')
parser.add_argument('--data_root', default='../data/optic_seg/', type=str)
parser.add_argument('--model', default='DeepLabv3p', type=str)
parser.add_argument('--backbone', default='resnet101', type=str)
parser.add_argument('--pretrained_resnet_path', default='./pretrained', type=str, help='load saved resnet') 
parser.add_argument('--num_workers', type=int, default=2, help='number of workers in dataloader')
parser.add_argument('--num_class', type=int, default=3, help='number of classes per dataset')
parser.add_argument('--dropout', type=float, default=0.5, help='dropout rate')
parser.add_argument('--resize_H', type=int, default=512, help='resize')
parser.add_argument('--resize_W', type=int, default=512, help='resize')
parser.add_argument('--loss_function', type=str, default='CE', help='loss')     # CE, MSE, nll

# param to be set
parser.add_argument('--epochs', type=int, default=300, help='number of epochs to train')
parser.add_argument('--pretrained_dataset', type=str, default='G1020', help='which source dataset to use')
parser.add_argument('--dataset', type=str, default='REFUGE', help='which dataset to use')
parser.add_argument('--seed', type=int, default=1, help='random seed (default: 1)')
parser.add_argument('--gpus', type=lambda s: [int(item.strip()) for item in s.split(',')], default='0',
                    help='comma delimited of gpu ids to use. Use "-1" for cpu usage')
parser.add_argument('--batch_size', type=int, default=1, help='Size of train batch')
parser.add_argument('--test_batch_size', type=int, default=1, help='Size of test batch')

# method
parser.add_argument('--bilinear', type=str2bool, default=False, help='bilinear in UNet')
parser.add_argument('--kernel_size', type=int, default=5, help='kernel size of erode noise')
parser.add_argument('--dilate_iteration', type=int, default=8, help='dilate iteration')

# optimizer
parser.add_argument('--base_lr', type=float, default=1e-3, help='learning rate')
parser.add_argument('--lr_backbone', type=float, default=1, help='backbone, 1x finetune')
parser.add_argument('--lr_network', type=float, default=10, help='network, 10x finetune')
parser.add_argument('--poly_power', type=float, default=0.9, help='poly scheduler')

# save path
parser.add_argument('--load_path', type=str, default='./save_source/G1020/test', help='load saved source model')
parser.add_argument('--save_vis_epoch', type=str2bool, default=False, help='save vis per epoch')
parser.add_argument('--out_path', type=str, default='./save_new_data/', help='log folder path')
parser.add_argument('--exp_name', type=str, default='test', help='Name of the experiment')

args = parser.parse_args()

# ==================
# init
# ==================
io = utils.log.IOStream(args)
io.cprint(str(args))

args.save_path = io.path

random.seed(1)
torch.manual_seed(args.seed)
args.cuda = (args.gpus[0] >= 0) and torch.cuda.is_available()
device = torch.device("cuda:" + str(args.gpus[0]) if args.cuda else "cpu")
if args.cuda:
    io.cprint('Using GPUs ' + str(args.gpus) + ',' + ' from ' +
              str(torch.cuda.device_count()) + ' devices available')
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.enabled = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
else:
    io.cprint('Using CPU')

# ==================
# Read Data
# ==================
def split_set(dataset):
    """
    Input:
        dataset
    output:
        train_sampler, valid_sampler
    """
    train_indices = dataset.train_ind
    val_indices = dataset.val_ind
    # Creating PT data samplers and loaders:
    train_sampler = SubsetRandomSampler(train_indices)
    valid_sampler = SubsetRandomSampler(val_indices)
    return train_sampler, valid_sampler

dataset = args.dataset

if dataset == 'G1020':
    dataset = G1020_Dataset(args.data_root, save_split_dir=args.exp_name, output_size=(args.resize_H, args.resize_W))
    train_sampler, val_sampler = split_set(dataset)

    train_loader = DataLoader(dataset, num_workers=args.num_workers, batch_size=args.batch_size, sampler=train_sampler, drop_last=False)
    val_loader = DataLoader(dataset, num_workers=args.num_workers, batch_size=args.test_batch_size, sampler=val_sampler)

elif dataset == 'REFUGE':
    trainset = REFUGE_Dataset(args.data_root, split='train', output_size=(args.resize_H, args.resize_W))
    valset = REFUGE_Dataset(args.data_root, split='val', output_size=(args.resize_H, args.resize_W))

    train_loader = DataLoader(trainset, num_workers=args.num_workers, batch_size=args.batch_size, shuffle=True, drop_last=False)
    val_loader = DataLoader(valset, num_workers=args.num_workers, batch_size=args.test_batch_size)

elif dataset == 'REFUGE2':
    trainset = REFUGE2_Dataset(args.data_root, split='train', output_size=(args.resize_H, args.resize_W))
    valset = REFUGE2_Dataset(args.data_root, split='val', output_size=(args.resize_H, args.resize_W))

    train_loader = DataLoader(trainset, num_workers=args.num_workers, batch_size=args.batch_size, shuffle=True, drop_last=False)
    val_loader = DataLoader(valset, num_workers=args.num_workers, batch_size=args.test_batch_size)

elif dataset == 'ORIGA':
    dataset = ORIGA_Dataset(args.data_root, save_split_dir=args.exp_name, output_size=(args.resize_H, args.resize_W))
    train_sampler, val_sampler = split_set(dataset)

    train_loader = DataLoader(dataset, num_workers=args.num_workers, batch_size=args.batch_size, sampler=train_sampler, drop_last=False)
    val_loader = DataLoader(dataset, num_workers=args.num_workers, batch_size=args.test_batch_size, sampler=val_sampler)

else:
    io.cprint('unknown dataset')

pretrained_dataset = args.pretrained_dataset

if pretrained_dataset == 'G1020':
    pretrained_dataset = G1020_Dataset(args.data_root, save_split_dir=args.exp_name)
    pretrained_train_sampler, pretrained_val_sampler = split_set(pretrained_dataset)

    pretrained_val_loader = DataLoader(pretrained_dataset, num_workers=args.num_workers, batch_size=args.test_batch_size, sampler=pretrained_val_sampler)

elif pretrained_dataset == 'REFUGE':
    pretrained_valset = REFUGE_Dataset(args.data_root, split='val')

    pretrained_val_loader = DataLoader(pretrained_valset, num_workers=args.num_workers, batch_size=args.test_batch_size)

elif pretrained_dataset == 'REFUGE2':
    pretrained_valset = REFUGE2_Dataset(args.data_root, split='val')

    pretrained_val_loader = DataLoader(pretrained_valset, num_workers=args.num_workers, batch_size=args.test_batch_size)

elif pretrained_dataset == 'ORIGA':
    pretrained_dataset = ORIGA_Dataset(args.data_root, save_split_dir=args.exp_name)
    pretrained_train_sampler, pretrained_val_sampler = split_set(pretrained_dataset)

    pretrained_val_loader = DataLoader(pretrained_dataset, num_workers=args.num_workers, batch_size=args.test_batch_size, sampler=pretrained_val_sampler)

else:
    io.cprint('unknown dataset')


# ==================
# Save target
# ==================

def create_optic_colormap():
    colormap = np.zeros((256, 3), dtype=np.uint8)
    colormap[1] = [128, 128, 128]
    colormap[2] = [255, 255, 255]

    return colormap


def colorize(mask, colormap):
    color_mask = np.zeros([mask.shape[0], mask.shape[1], 3])
    for i in np.unique(mask):
        color_mask[mask == i] = colormap[i]

    return Image.fromarray(np.uint8(color_mask))


count = 0

batch_idx = 0

# --------------------- #
# save train data
# --------------------- #
new_data = []
new_noisy_label = []
new_clean_label = []
new_data_name = []

colormap = create_optic_colormap()

for data_all in train_loader:
    data, labels, data_name = data_all[0].to(device), data_all[1].long().to(device).squeeze(), data_all[2]

    batch_size, _, img_H, img_W = data.shape

    if len(labels.shape) < 3:
        labels = labels.unsqueeze(0)

    for ii in range(batch_size):
        # OD
        mask_bg_OD = torch.zeros([img_H, img_W]).to(device)
        mask_bg_OD[labels[ii] == 1] = 1

        mask_bg_OD = mask_bg_OD.detach().cpu().numpy()
        kernel = np.ones((args.kernel_size, args.kernel_size), np.uint8)
        
        if mask_bg_OD.sum() > 0:
            if random.random() < 0.5:
                mask_bg_OD_ed = cv2.erode(mask_bg_OD, kernel)
            else:
                mask_bg_OD_ed = cv2.dilate(mask_bg_OD, kernel, iterations=args.dilate_iteration)
        else:
            mask_bg_OD_ed = mask_bg_OD

        # OC
        mask_bg_OC = torch.zeros([img_H, img_W]).to(device)
        mask_bg_OC[labels[ii] == 2] = 1

        mask_bg_OC = mask_bg_OC.detach().cpu().numpy()
        kernel = np.ones((args.kernel_size, args.kernel_size), np.uint8)

        if mask_bg_OC.sum() > 0:
            if random.random() < 0.5:
                mask_bg_OC_ed = cv2.erode(mask_bg_OC, kernel)
            else:
                mask_bg_OC_ed = cv2.dilate(mask_bg_OC, kernel, iterations=args.dilate_iteration)
        else:
            mask_bg_OC_ed = mask_bg_OC

        mask_bg_OD_ed = torch.Tensor(mask_bg_OD_ed)
        mask_bg_OC_ed = torch.Tensor(mask_bg_OC_ed)

        mask_ed = torch.zeros([img_H, img_W])

        # in case overlap exist
        if random.random() < 0.5:
            mask_ed[mask_bg_OD_ed == 1] = 1
            mask_ed[mask_bg_OC_ed == 1] = 2
        else:
            mask_ed[mask_bg_OC_ed == 1] = 2
            mask_ed[mask_bg_OD_ed == 1] = 1

        # --------------------------------------------- #
        # save new data
        if len(new_data) == 0:
            new_data = data[ii].unsqueeze(0)
            new_noisy_label = mask_ed.unsqueeze(0)
            new_clean_label = labels[ii].unsqueeze(0)
            new_data_name.append(data_name[ii])
        else:
            new_data = torch.cat([new_data, data[ii].unsqueeze(0)], dim=0)
            new_noisy_label = torch.cat([new_noisy_label, mask_ed.unsqueeze(0)], dim=0)
            new_clean_label = torch.cat([new_clean_label, labels[ii].unsqueeze(0)], dim=0)
            new_data_name.append(data_name[ii])

        # --------------------------------------------- #
        # save noisy vis
        gray = np.uint8(mask_ed.cpu().numpy())
        color = colorize(gray, colormap)
        image_name = data_name[ii].split('.')[0]          # eg: 414.jpg --> 414
        color_folder = args.save_path + '/train_vis/'
        os.makedirs(color_folder, exist_ok=True)
        color_path = os.path.join(color_folder, image_name + "_noisy.png")
        gray = Image.fromarray(gray)
        color.save(color_path)

        # save gt vis
        gray = np.uint8(labels[ii].cpu().numpy())
        color = colorize(gray, colormap)
        image_name = data_name[ii].split('.')[0]          # eg: 414.jpg --> 414
        color_folder = args.save_path + '/train_vis/'
        os.makedirs(color_folder, exist_ok=True)
        color_path = os.path.join(color_folder, image_name + "_gt.png")
        gray = Image.fromarray(gray)
        color.save(color_path)

        # save raw vis
        toPIL = transforms.ToPILImage()
        std = torch.Tensor([0.229, 0.224, 0.225]).to(device)
        mean = torch.Tensor([0.485, 0.456, 0.406]).to(device)
        un_norm = transforms.Normalize((-mean / std).tolist(), (1.0 / std).tolist())
        save_data = un_norm(data[ii])
        pic = toPIL(save_data)
        raw_path = os.path.join(color_folder, image_name + "_raw.jpg")
        pic.save(raw_path) 

    count += batch_size
    batch_idx += 1

new_data = new_data.detach().cpu().numpy()
new_noisy_label = new_noisy_label.detach().cpu().numpy()
new_clean_label = new_clean_label.detach().cpu().numpy()
new_data_name = np.array(new_data_name)

# --------------------- #
# save val data
# --------------------- #
new_val_data = []
new_val_clean_label = []
new_val_data_name = []

for data_all in val_loader:
    data, labels, data_name = data_all[0], data_all[1].long().squeeze(), data_all[2]

    batch_size, _, img_H, img_W = data.shape

    if len(labels.shape) < 3:
        labels = labels.unsqueeze(0)

    for ii in range(batch_size):
        
        # ------------------------------------------------------------------ #
        # save new data
        if len(new_val_data) == 0:
            new_val_data = data[ii].unsqueeze(0)
            new_val_clean_label = labels[ii].unsqueeze(0)
            new_val_data_name.append(data_name[ii])
        else:
            new_val_data = torch.cat([new_val_data, data[ii].unsqueeze(0)], dim=0)
            new_val_clean_label = torch.cat([new_val_clean_label, labels[ii].unsqueeze(0)], dim=0)
            new_val_data_name.append(data_name[ii])

    count += batch_size
    batch_idx += 1

new_val_data = new_val_data.detach().numpy()
new_val_clean_label = new_val_clean_label.detach().numpy()
new_val_data_name = np.array(new_val_data_name)

# save data
save_data_path = args.save_path + '/train_data/'
os.makedirs(save_data_path, exist_ok=True)
np.save(save_data_path + 'new_data.npy', new_data)
np.save(save_data_path + 'new_noisy_label.npy', new_noisy_label)
np.save(save_data_path + 'new_clean_label.npy', new_clean_label)
np.save(save_data_path + 'new_data_name.npy', new_data_name)

io.cprint("save noisy train data for %s" % (args.dataset))

save_val_data_path = args.save_path + '/val_data/'
os.makedirs(save_val_data_path, exist_ok=True)
np.save(save_val_data_path + 'new_val_data.npy', new_val_data)
np.save(save_val_data_path + 'new_val_clean_label.npy', new_val_clean_label)
np.save(save_val_data_path + 'new_val_data_name.npy', new_val_data_name)

io.cprint("save val data for %s" % (args.dataset))
io.cprint("+++++++++++++++++++++++++end of training+++++++++++++++++++++++++")
io.cprint("+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++")
