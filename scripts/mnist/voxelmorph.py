import os
import torch
import numpy as np
import torch.nn as nn
from scripts.mnist.utils import to_nametuple, Grad2D,  multi_props_inj, prop_inj
from scripts.mnist.data_loader import MNISTData, BrainData
from torchsummary import summary
from tqdm import tqdm

os.environ['VXM_BACKEND'] = 'pytorch'
import voxelmorph as vxm


class VoxelMNIST(nn.Module):
    def __init__(self, inshape, nb_features, ndim):
        super().__init__()
        self.unet = vxm.networks.Unet(inshape=inshape, nb_features=nb_features)
        self.flow = nn.Conv2d(nb_features[1][-1], ndim, 3, padding=1)
        self.transformer = vxm.layers.SpatialTransformer(inshape)

    def forward(self, source, target):
        x = torch.cat([source, target], dim=1)
        x = self.unet(x)
        flow_field = self.flow(x)
        moving_transformed = self.transformer(source, flow_field)
        return moving_transformed, flow_field


def build_vxm(data_name, config, device="cpu"):
    # unet architecture
    if data_name == "mnist":
        model = VoxelMNIST(config.inshape, config.nb_features, config.ndim)
    else: 
        model = vxm.networks.VxmDense(config.inshape, config.nb_features, int_steps=0)
        
    # prepare the model for training and send to device
    model.to(device)
    model.train()

    # set optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=config.lr)

    # prepare image loss
    if config.image_loss == 'ncc':
        image_loss_func = vxm.losses.NCC().loss
    elif config.image_loss == 'mse':
        image_loss_func = vxm.losses.MSE().loss
    else:
        raise ValueError('Image loss should be "mse" or "ncc", but found "%s"' % config.image_loss)

    L_sim = image_loss_func
    L_smooth = Grad2D('l2').loss

    return to_nametuple({'model': model, 'optimizer': optimizer,
                         'losses': {'sim': L_sim, 'smooth': L_smooth}})


def train_vxm(config, trainer, train_data, test_data, verbose=True, device="cpu"):
    loss_hist = []
    # training loops
    zero_phi = np.zeros([config.batch_size_train, *config.inshape, config.ndim])

    for epoch in tqdm(range(config.epochs)):
        for step in range(config.steps_per_epoch):
            # generate inputs (and true outputs) and convert them to tensors
            x_fix, x_mvt = next(train_data['fix']), next(train_data['moving'])
            size = min(x_fix.shape[0], x_mvt.shape[0])
            x_fix, x_mvt = x_fix[:size].to(device), x_mvt[:size].to(device)  # because the remaining batch element can have diff size
            inputs = [x_mvt, x_fix]

            # run inputs through the model to produce a warped image and flow field
            x_pred, flow = trainer.model(*inputs)

            # calculate total loss
            loss_sim = trainer.losses['sim'](x_pred, x_fix)
            loss_smooth = config.λ * trainer.losses['smooth'](None, flow)
            loss = loss_sim + loss_smooth

            loss_info = 'loss: %.6f ' % loss.item()

            # backpropagate and optimize

            trainer.optimizer.zero_grad()
            loss.backward()
            trainer.optimizer.step()

            # print step info
            if verbose:
                if step % config.steps_per_epoch == 0:
                    epoch_info = 'epoch: %04d' % (epoch + 1)
                    step_info = ('step: %d/%d' % (step + 1, config.steps_per_epoch)).ljust(14)
                    print('  '.join((epoch_info, step_info, loss_info)), flush=True)

        loss_hist.append(loss.item())
    #inj_score = 100 * (1 - multi_props_inj(trainer, test_data, 'vxm'))

    return loss_hist #, inj_score


def load_vxm(data_name, path, device='cpu'):
    checkpoint = torch.load(path, map_location=device)
    conf = to_nametuple(checkpoint['config'])
    hist = checkpoint['hist']
    #inj = checkpoint['inj']

    trainer = build_vxm(data_name, conf, device)
    trainer.model.load_state_dict(checkpoint['model_state_dict'])
    trainer.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    trainer.model.to(device)
    print(f'model load on device: {device}')

    return conf, trainer, hist#, inj


def train(data_name, conf, device="cpu", save=True, save_name='default', save_folder='output', verbose=True):
    print(f'train on {device}')
    if device=="cuda":
        os.environ['CUDA_VISIBLE_DEVICES'] = '0'
        torch.backends.cudnn.deterministic = True
        
    # load data
    if data_name=="mnist":
        mnist_data = MNISTData()
        x_train, x_val = mnist_data.train_val(conf.fix, conf.moving)
        x_test = mnist_data.test_data(conf.fix, conf.moving)
    elif data_name == "brain":
        brain_data = BrainData()
        x_train, x_val = brain_data.train_val()
        x_test = brain_data.test_data()
    else:
        assert False, f"wrong data name: {data_name}"

    # build model
    trainer = build_vxm(data_name, conf, device)

    if verbose:
        print(summary(trainer.model, [(1, *conf.inshape), (1, *conf.inshape)]))

    # train model
    #hist, inj_score = train_vxm(conf, trainer, x_train, x_test, verbose, device)
    hist= train_vxm(conf, trainer, x_train, x_test, verbose, device)
    

    # save model
    if save:
        torch.save({'config': dict(conf._asdict()),
                    'hist': hist,
                    #'inj': inj_score,
                    'model_state_dict': trainer.model.state_dict(),
                    'optimizer_state_dict': trainer.optimizer.state_dict(),
                    }, os.path.join(save_folder, f'model-{data_name}-vxm-{save_name}.pt'))

    return trainer
