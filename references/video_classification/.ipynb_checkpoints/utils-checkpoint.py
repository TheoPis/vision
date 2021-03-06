from __future__ import print_function
from collections import defaultdict, deque
import datetime
import time
import torch
import torch.distributed as dist

import errno
import os

import sys

def info(type, value, tb):
    if hasattr(sys, 'ps1') or not sys.stderr.isatty():
    # we are in interactive mode or we don't have a tty-like
    # device, so we call the default hook
        sys.__excepthook__(type, value, tb)
    else:
        import traceback, pdb
        # we are NOT in interactive mode, print the exception...
        traceback.print_exception(type, value, tb)
        print
        # ...then start the debugger in post-mortem mode.
        # pdb.pm() # deprecated
        pdb.post_mortem(tb) # more "modern"

sys.excepthook = info


class SmoothedValue(object):
    """Track a series of values and provide access to smoothed values over a
    window or the global series average.
    """

    def __init__(self, window_size=20, fmt=None):
        if fmt is None:
            fmt = "{median:.4f} ({global_avg:.4f})"
        self.deque = deque(maxlen=window_size)
        self.total = 0.0
        self.count = 0
        self.fmt = fmt

    def update(self, value, n=1):
        self.deque.append(value)
        self.count += n
        self.total += value * n

    def synchronize_between_processes(self):
        """
        Warning: does not synchronize the deque!
        """
        if not is_dist_avail_and_initialized():
            return
        t = torch.tensor([self.count, self.total], dtype=torch.float64, device='cuda')
        dist.barrier()
        dist.all_reduce(t)
        t = t.tolist()
        self.count = int(t[0])
        self.total = t[1]

    @property
    def median(self):
        d = torch.tensor(list(self.deque))
        return d.median().item()

    @property
    def avg(self):
        d = torch.tensor(list(self.deque), dtype=torch.float32)
        return d.mean().item()

    @property
    def global_avg(self):
        return self.total / self.count

    @property
    def max(self):
        return max(self.deque)

    @property
    def value(self):
        return self.deque[-1]

    def __str__(self):
        return self.fmt.format(
            median=self.median,
            avg=self.avg,
            global_avg=self.global_avg,
            max=self.max,
            value=self.value)


class MetricLogger(object):
    def __init__(self, delimiter="\t"):
        self.meters = defaultdict(SmoothedValue)
        self.delimiter = delimiter

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if isinstance(v, torch.Tensor):
                v = v.item()
            assert isinstance(v, (float, int))
            self.meters[k].update(v)

    def __getattr__(self, attr):
        if attr in self.meters:
            return self.meters[attr]
        if attr in self.__dict__:
            return self.__dict__[attr]
        raise AttributeError("'{}' object has no attribute '{}'".format(
            type(self).__name__, attr))

    def __str__(self):
        loss_str = []
        for name, meter in self.meters.items():
            loss_str.append(
                "{}: {}".format(name, str(meter))
            )
        return self.delimiter.join(loss_str)

    def synchronize_between_processes(self):
        for meter in self.meters.values():
            meter.synchronize_between_processes()

    def add_meter(self, name, meter):
        self.meters[name] = meter

    def log_every(self, iterable, print_freq, header=None):
        i = 0
        if not header:
            header = ''
        start_time = time.time()
        end = time.time()
        iter_time = SmoothedValue(fmt='{avg:.4f}')
        data_time = SmoothedValue(fmt='{avg:.4f}')
        space_fmt = ':' + str(len(str(len(iterable)))) + 'd'
        if torch.cuda.is_available():
            log_msg = self.delimiter.join([
                header,
                '[{0' + space_fmt + '}/{1}]',
                'eta: {eta}',
                '{meters}',
                'time: {time}',
                'data: {data}',
                'max mem: {memory:.0f}'
            ])
        else:
            log_msg = self.delimiter.join([
                header,
                '[{0' + space_fmt + '}/{1}]',
                'eta: {eta}',
                '{meters}',
                'time: {time}',
                'data: {data}'
            ])
        MB = 1024.0 * 1024.0
        for obj in iterable:
            data_time.update(time.time() - end)
            yield obj
            iter_time.update(time.time() - end)
            if i % print_freq == 0:
                eta_seconds = iter_time.global_avg * (len(iterable) - i)
                eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))
                if torch.cuda.is_available():
                    print(log_msg.format(
                        i, len(iterable), eta=eta_string,
                        meters=str(self),
                        time=str(iter_time), data=str(data_time),
                        memory=torch.cuda.max_memory_allocated() / MB))
                else:
                    print(log_msg.format(
                        i, len(iterable), eta=eta_string,
                        meters=str(self),
                        time=str(iter_time), data=str(data_time)))
            i += 1
            end = time.time()
        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print('{} Total time: {}'.format(header, total_time_str))


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target[None])

        res = []
        for k in topk:
            correct_k = correct[:k].flatten().sum(dtype=torch.float32)
            res.append(correct_k * (100.0 / batch_size))
        return res


def mkdir(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def setup_for_distributed(is_master):
    """
    This function disables printing when not in master process
    """
    import builtins as __builtin__
    builtin_print = __builtin__.print

    def print(*args, **kwargs):
        force = kwargs.pop('force', False)
        if is_master or force:
            builtin_print(*args, **kwargs)

    __builtin__.print = print


def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True


def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()


def get_rank():
    if not is_dist_avail_and_initialized():
        return 0
    return dist.get_rank()


def is_main_process():
    return get_rank() == 0


def save_on_master(*args, **kwargs):
    if is_main_process():
        torch.save(*args, **kwargs)


def init_distributed_mode(args):
    if 'RANK' in os.environ and 'WORLD_SIZE' in os.environ:
        args.rank = int(os.environ["RANK"])
        args.world_size = int(os.environ['WORLD_SIZE'])
        args.gpu = int(os.environ['LOCAL_RANK'])
    elif 'SLURM_PROCID' in os.environ:
        args.rank = int(os.environ['SLURM_PROCID'])
        args.gpu = args.rank % torch.cuda.device_count()
    elif hasattr(args, "rank"):
        pass
    else:
        print('Not using distributed mode')
        args.distributed = False
        return

    args.distributed = True

    torch.cuda.set_device(args.gpu)
    args.dist_backend = 'nccl'
    print('| distributed init (rank {}): {}'.format(
        args.rank, args.dist_url), flush=True)
    torch.distributed.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                         world_size=args.world_size, rank=args.rank)
    setup_for_distributed(args.rank == 0)


import math
import numbers
from torch import nn
from torch.nn import functional as F

class GaussianSmoothing(nn.Module):
    """
    Apply gaussian smoothing on a
    1d, 2d or 3d tensor. Filtering is performed seperately for each channel
    in the input using a depthwise convolution.
    Arguments:
        channels (int, sequence): Number of channels of the input tensors. Output will
            have this number of channels as well.
        kernel_size (int, sequence): Size of the gaussian kernel.
        sigma (float, sequence): Standard deviation of the gaussian kernel.
        dim (int, optional): The number of dimensions of the data.
            Default value is 2 (spatial).
    """
    def __init__(self, channels, kernel_size, sigma, dim=2):
        super(GaussianSmoothing, self).__init__()
        if isinstance(kernel_size, numbers.Number):
            kernel_size = [kernel_size] * dim
        if isinstance(sigma, numbers.Number):
            sigma = [sigma] * dim

        # The gaussian kernel is the product of the
        # gaussian function of each dimension.
        kernel = 1
        meshgrids = torch.meshgrid(
            [
                torch.arange(size, dtype=torch.float32)
                for size in kernel_size
            ]
        )
        for size, std, mgrid in zip(kernel_size, sigma, meshgrids):
            mean = (size - 1) / 2
            kernel *= 1 / (std * math.sqrt(2 * math.pi)) * \
                      torch.exp(-((mgrid - mean) / (2 * std)) ** 2)

        # Make sure sum of values in gaussian kernel equals 1.
        kernel = kernel / torch.sum(kernel)

        # Reshape to depthwise convolutional weight
        kernel = kernel.view(1, 1, *kernel.size())
        kernel = kernel.repeat(channels, *[1] * (kernel.dim() - 1))

        self.register_buffer('weight', kernel)
        self.groups = channels

        if dim == 1:
            self.conv = F.conv1d
        elif dim == 2:
            self.conv = F.conv2d
        elif dim == 3:
            self.conv = F.conv3d
        else:
            raise RuntimeError(
                'Only 1, 2 and 3 dimensions are supported. Received {}.'.format(dim)
            )

    def forward(self, input):
        """
        Apply gaussian filter to input.
        Arguments:
            input (torch.Tensor): Input to apply gaussian filter on.
        Returns:
            filtered (torch.Tensor): Filtered output.
        """
        return self.conv(input, weight=self.weight, groups=self.groups)



# Video's features
import numpy as np
from sklearn.decomposition import PCA
import cv2
import imageio as io

def pca_feats(ff, solver='auto', img_normalize=True):
    ## expect ff to be   N x C x H x W
    
    N, C, H, W = ff.shape
    pca = PCA(
        n_components=3,
        svd_solver=solver,
        whiten=False
    )
#     print(ff.shape)
    ff = ff.transpose(1, 2).transpose(2, 3)
#     print(ff.shape)
    ff = ff.reshape(N*H*W, C).numpy()
#     print(ff.shape)
    
    pca_ff = torch.Tensor(pca.fit_transform(ff))
    pca_ff = pca_ff.view(N, H, W, 3)
#     print(pca_ff.shape)
    pca_ff = pca_ff.transpose(3, 2).transpose(2, 1)

    if img_normalize:
        pca_ff = (pca_ff - pca_ff.min()) / (pca_ff.max() - pca_ff.min())

    return pca_ff


def make_gif(video, outname='/tmp/test.gif'):
    if hasattr(video, 'shape'):
        video = video.cpu()
        if video.shape[0] == 3:
            video = video.transpose(0, 1)

        video = video.numpy().transpose(0, 2, 3, 1)
        # print(video.min(), video.max())
        
        video = (video*255).astype(np.uint8)
#         video = video.chunk(video.shape[0])
        
    video = [cv2.resize(vv, (256, 256)) for vv in video]

    if outname is None:
        return np.stack(video)

    io.mimsave(outname, video, duration = 0.2)


def compute_RF_numerical(net,img_np):
    '''
    @param net: Pytorch network
    @param img_np: numpy array to use as input to the networks, it must be full of ones and with the correct
    shape.
    '''
    def weights_init(m):
        classname = m.__class__.__name__
        # if classname.find('Pad') != -1:
        #     m = list(m.modules())[-1]
        #     m.weight.data.fill_(1)
        #     if hasattr(m, 'bias') and m.bias is not None:
        #         m.bias.data.fill_(0)
        # print(classname)
        if classname == 'Conv3d':
            print(m)
            m.weight.data.fill_(1)
            if hasattr(m, 'bias') and m.bias is not None:
                m.bias.data.fill_(0)

    net.apply(weights_init)
    img_ = torch.from_numpy(img_np).float()
    img_.requires_grad = True
    
    out_cnn = net(img_)
    out_shape = out_cnn.size()
    ndims = len(out_cnn.size())
    grad = torch.zeros(out_cnn.size())
    l_tmp=[]

    # import pdb; pdb.set_trace()
    for i in range(ndims):
        if i==0 or i ==1 or i ==2:#batch or channel
            l_tmp.append(0)
        else:
            l_tmp.append(out_shape[i]//2)
            
    grad[tuple(l_tmp)]=100

    import pdb; pdb.set_trace()

    out_cnn.backward(grad)
    grad_np=img_.grad[0,0].data.numpy()
    idx_nonzeros=np.where(grad_np!=0)
    RF=[np.max(idx)-np.min(idx)+1 for idx in idx_nonzeros]
    
    return RF

import visdom
import time

class Visualize(object):
    def __init__(self, args, log_interval=10):
        self.vis = visdom.Visdom(port=8095, env="%s_metrics" % args.name)
        
        self.data = dict()

        self.log_interval = log_interval
        self._last_flush = time.time()

    def log(self, key, value):
        if not key in self.data:
            self.data[key] = [[],[]]

        if isinstance(value, tuple):
            self.data[key][0].append(value[0])
            self.data[key][1].append(value[1])
        else:
            self.data[key][1].append(value)
            self.data[key][0].append(len(self.data[key][1]) * 1.0)
            # import pdb; pdb.set_trace()

        if (time.time() - self._last_flush) > (self.log_interval):
            for k in self.data:
                self.vis.line(
                    X=np.array(self.data[k][0]),
                    Y=np.array(self.data[k][1]),
                    win=k,
                    opts=dict( title=k )
                )
            self._last_flush = time.time()


import torchvision
import transforms as T

def make_frame_transform(frame_transform_string):
    tt = []
    fts = frame_transform_string.split(',')

    # torchvision.transforms.RandomAffine(degrees, translate=None, scale=None, shear=None, resample=False, fillcolor=0),
    # torchvision.transforms.RandomGrayscale(p=0.1)
    # torchvision.transforms.RandomHorizontalFlip(p=0.5)
    # torchvision.transforms.RandomPerspective(distortion_scale=0.5, p=0.5, interpolation=3)
    # torchvision.transforms.Resize(256, 256),
    
    # import pdb; pdb.set_trace()
    if 'blur' in fts:
        tt += [torchvision.transforms.ToTensor(), T.GaussianBlurTransform(), torchvision.transforms.ToPILImage()]
        
    if 'gray' in fts:
        tt.append(torchvision.transforms.RandomGrayscale(p=1))
    
    if 'flip' in fts:
        tt.append(torchvision.transforms.RandomHorizontalFlip(p=0.3))

    if 'crop' in fts:
        tt.append(torchvision.transforms.RandomResizedCrop(
            256, scale=(0.6, 0.9), ratio=(0.7, 1.3), interpolation=2),)

    frame_transform_train = T.PerTimestepTransform(
            torchvision.transforms.Compose(tt)
        )
    print('Frame transforms:', tt)
    return frame_transform_train


# def vis_flow(u, v):
#     flows = []
#     u, v = u.data.cpu().numpy().astype(np.float32), v.data.cpu().numpy().astype(np.float32)
#     hsv = np.zeros((u.shape[1], u.shape[2], 3))
    
#     for i in range(u.shape[0]):
#         mag, ang = cv2.cartToPolar(u[i], v[i])
#         hsv[...,1] = 255
#         hsv[...,0] = ang*180/np.pi/2
#         hsv[...,2] = cv2.normalize(mag,None,0,255,cv2.NORM_MINMAX)
#         bgr = cv2.cvtColor(hsv.astype(np.float32), cv2.COLOR_HSV2BGR)
#         flows.append(bgr)

#     return flows

# def compute_flow(corr):
#     nnf = corr.argmax(dim=1)
#     # nnf = nnf.transpose(-1, -2)

#     u = nnf % nnf.shape[-1]
#     v = nnf / nnf.shape[-2] # nnf is an IntTensor so rounds automatically

#     rr = torch.arange(u.shape[-1])[None].long().cuda()

#     for i in range(u.shape[-1]):
#         u[:, i] -= rr

#     for i in range(v.shape[-1]):
#         v[:, :, i] -= rr

#     flows = vis_flow(u, v)

#     return flows, u, v


def compute_flow(corr):
    # assume corr is shape N x H * W x W x H
    nnf = corr.argmax(dim=1)
    nnf = nnf.transpose(-1, -2)

    u = nnf % nnf.shape[-1]
    v = nnf / nnf.shape[-2] # nnf is an IntTensor so rounds automatically

    rr = torch.arange(u.shape[-1])[None].long().cuda()

    for i in range(u.shape[-1]):
        u[:, i] -= rr

    for i in range(v.shape[-1]):
        v[:, :, i] -= rr

    return u, v
        
import cv2
import numpy as np

def draw_flow(img, flow, step=16):
    h, w = img.shape[:2]
    y, x = np.mgrid[step/2:h:step, step/2:w:step].reshape(2,-1)
    fx, fy = flow[y,x].T
    lines = np.vstack([x, y, x+fx, y+fy]).T.reshape(-1, 2, 2)
    lines = np.int32(lines + 0.5)
    vis = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    cv2.polylines(vis, lines, 0, (0, 255, 0))
    for (x1, y1), (x2, y2) in lines:
        cv2.circle(vis, (x1, y1), 1, (0, 255, 0), -1)
    return vis

def draw_hsv(flow):
    h, w = flow.shape[:2]
    fx, fy = flow[:,:,0], flow[:,:,1]
    ang = np.arctan2(fy, fx) + np.pi
    v = np.sqrt(fx*fx+fy*fy)
    hsv = np.zeros((h, w, 3), np.uint8)
    hsv[...,0] = ang*(180/np.pi/2)
    hsv[...,1] = 255
    hsv[...,2] = np.minimum(v*4, 255)
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)#[::-1]
    return bgr
