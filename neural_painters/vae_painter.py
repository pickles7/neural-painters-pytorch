"""Contains classes and functions for a VAE neural painter"""
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

import gdown

from neural_painters.data import FullActionStrokeDataLoader
from neural_painters.common import reconstruction_loss_function


class VAEEncoder(nn.Module):
  def __init__(self, z_size):
    super(VAEEncoder, self).__init__()

    self.conv1 = nn.Conv2d(3, 32, 4, stride=2)
    self.conv2 = nn.Conv2d(32, 64, 4, stride=2)
    self.conv3 = nn.Conv2d(64, 128, 4, stride=2)
    self.conv4 = nn.Conv2d(128, 256, 4, stride=2)
    self.fc_mu = nn.Linear(2 * 2 * 256, z_size)
    self.fc_log_var = nn.Linear(2 * 2 * 256, z_size)

  def forward(self, x):
    x = F.relu(self.conv1(x))
    x = F.relu(self.conv2(x))
    x = F.relu(self.conv3(x))
    x = F.relu(self.conv4(x))
    x = x.view(-1, 2 * 2 * 256)
    mu = self.fc_mu(x)
    log_var = self.fc_log_var(x)

    # reparameterization
    std = torch.exp(0.5 * log_var)
    eps = torch.randn_like(std)
    z = mu + eps * std

    return z, mu, log_var


class VAEDecoder(nn.Module):
  def __init__(self, z_size):
    super(VAEDecoder, self).__init__()

    self.fc = nn.Linear(z_size, 4 * 256)
    self.deconv1 = nn.ConvTranspose2d(1024, 128, 5, stride=2)
    self.deconv2 = nn.ConvTranspose2d(128, 64, 5, stride=2)
    self.deconv3 = nn.ConvTranspose2d(64, 32, 6, stride=2)
    self.deconv4 = nn.ConvTranspose2d(32, 3, 6, stride=2)

  def forward(self, x):
    x = self.fc(x)  # No activation?
    x = x.view(-1, 4 * 256, 1, 1)
    x = F.relu(self.deconv1(x))
    x = F.relu(self.deconv2(x))
    x = F.relu(self.deconv3(x))
    x = torch.sigmoid(self.deconv4(x))
    return x


class VAEPredictor(nn.Module):
  def __init__(self, action_size, z_size):
    super(VAEPredictor, self).__init__()

    self.fc1 = nn.Linear(action_size, 256)
    self.bn1 = nn.BatchNorm1d(256)
    self.fc2 = nn.Linear(256, 64)
    self.bn2 = nn.BatchNorm1d(64)
    self.fc3 = nn.Linear(64, 64)
    self.bn3 = nn.BatchNorm1d(64)
    self.fc_mu = nn.Linear(64, z_size)
    self.fc_log_var = nn.Linear(64, z_size)

  def forward(self, x):
    x = self.bn1(F.leaky_relu(self.fc1(x)))
    x = self.bn2(F.leaky_relu(self.fc2(x)))
    x = self.bn3(F.leaky_relu(self.fc3(x)))
    mu = self.fc_mu(x)
    log_var = self.fc_log_var(x)

    # reparameterization
    std = torch.exp(0.5 * log_var)
    eps = torch.randn_like(std)
    z = mu + eps * std

    return z, mu, log_var


class VAENeuralPainter(nn.Module):
  """VAE Neural Painter nn.Module for inference"""
  def __init__(self, action_size, z_size, stochastic=True, pretrained=False):
    """
    :param action_size: number of dimensions of action
    :param z_size: latent space dimension
    :param stochastic: make neural painter stochastic
    :param pretrained: use pretrained neural painter. Pretrained neural painter has action_size=12 and z_size=64
    """
    super(VAENeuralPainter, self).__init__()

    self.stochastic = stochastic
    self.predictor = VAEPredictor(action_size, z_size)
    self.decoder = VAEDecoder(z_size)

    if pretrained:
      url = 'https://drive.google.com/uc?id=1ETysXz9xFIooMlVvwlo5erENmU9JkKMv'
      output = os.path.join(os.path.expanduser('~'), '.cache', 'neural_painters', 'checkpoints', 'vae_neural_painter_latest.tar')
      if os.path.exists(output):
        print('Using cached checkpoint at {}'.format(output))
      else:
        os.makedirs(os.path.dirname(output), exist_ok=True)
        gdown.download(url, output, quiet=False)
        print('Downloaded pretrained checkpoint to {}'.format(output))
      self.load_from_train_checkpoint(output)

  def forward(self, x):
    z, mu, log_var = self.predictor(x)
    if self.stochastic:
      return self.decoder(z)
    else:
      return self.decoder(mu)

  def load_from_train_checkpoint(self, ckpt_path):
    checkpoint = torch.load(ckpt_path)
    self.decoder.load_state_dict(checkpoint['decoder_state_dict'])
    self.predictor.load_state_dict(checkpoint['predictor_state_dict'])
    print('Loaded from {}. Batch {}'.format(ckpt_path, checkpoint['batch_idx']))


def kl_loss_function(mu, logvar, kl_tolerance, z_size):
  # see Appendix B from VAE paper:
  # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
  # https://arxiv.org/abs/1312.6114
  # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
  KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=1)
  KLD = torch.max(KLD, torch.ones_like(KLD) * kl_tolerance * z_size)
  KLD = torch.mean(KLD)
  return KLD


def save_train_checkpoint(savedir: str,
                          name: str,
                          batch_idx: int,
                          encoder: VAEEncoder,
                          decoder: VAEDecoder,
                          predictor: VAEPredictor,
                          optimizer1,
                          optimizer2):
  os.makedirs(savedir, exist_ok=True)
  obj_to_save = {
    'batch_idx': batch_idx,
    'encoder_state_dict': encoder.state_dict(),
    'decoder_state_dict': decoder.state_dict(),
    'predictor_state_dict': predictor.state_dict(),
    'optimizer1_state_dict': optimizer1.state_dict(),
    'optimizer2_state_dict': optimizer2.state_dict()
  }
  torch.save(obj_to_save, os.path.join(savedir, '{}_{}.tar'.format(name, batch_idx)))
  torch.save(obj_to_save, os.path.join(savedir, '{}_latest.tar'.format(name)))
  print('saved {}'.format('{}_{}.tar'.format(name, batch_idx)))


def load_from_latest_checkpoint(savedir: str,
                                name: str,
                                encoder: VAEEncoder,
                                decoder: VAEDecoder,
                                predictor: VAEPredictor,
                                optimizer1,
                                optimizer2):
  latest_path = os.path.join(savedir, '{}_latest.tar'.format(name))
  if not os.path.exists(latest_path):
    print('{} not found. starting training from scratch'.format(latest_path))
    return -1
  checkpoint = torch.load(latest_path)
  encoder.load_state_dict(checkpoint['encoder_state_dict'])
  decoder.load_state_dict(checkpoint['decoder_state_dict'])
  predictor.load_state_dict(checkpoint['predictor_state_dict'])
  optimizer1.load_state_dict(checkpoint['optimizer1_state_dict'])
  optimizer2.load_state_dict(checkpoint['optimizer2_state_dict'])
  print('Loaded from {}. Batch {}'.format(latest_path, checkpoint['batch_idx']))
  return checkpoint['batch_idx']


def train_vae_neural_painter(z_size: int,
                             action_size: int,
                             batch_size: int,
                             kl_tolerance: float,
                             device: torch.device,
                             data_dir: str,
                             vae_train_steps: int = 300000,
                             save_every_n_steps: int = 25000,
                             log_every_n_steps: int = 2000,
                             tensorboard_every_n_steps: int = 100,
                             tensorboard_log_dir: str = 'logdir',
                             save_dir: str = 'vae_train_checkpoints',
                             save_name: str = 'vae_neural_painter'):
  """Trains VAE neural painter in two parts. Refer to the paper for details.

  :param z_size: latent space size
  :param action_size: action dimension
  :param batch_size: batch size used in training
  :param kl_tolerance: KL tolerance
  :param device: torch.device used in training
  :param data_dir: where the training data is stored
  :param vae_train_steps: How many training steps for first part i.e. VAE train steps
  :param save_every_n_steps: save checkpoint every n steps
  :param log_every_n_steps: print a log every n steps
  :param tensorboard_every_n_steps: log to tensorboard every n steps
  :param tensorboard_log_dir: tensorboard log directory
  :param save_dir: save directory for checkpoints
  :param save_name: save name used for extra identification
  """
  # Initialize data loader
  loader = FullActionStrokeDataLoader(data_dir, batch_size, False)

  # Initialize networks and optimizers
  encoder = VAEEncoder(z_size).to(device).train()
  decoder = VAEDecoder(z_size).to(device).train()
  predictor = VAEPredictor(action_size, z_size).to(device).train()

  optimizer1 = optim.Adam(list(encoder.parameters()) + list(decoder.parameters()), lr=1e-4)
  optimizer2 = optim.Adam(predictor.parameters(), lr=1e-4)

  # Initialize networks from latest checkpoint if it exists.
  batch_idx_offset = 1 + load_from_latest_checkpoint(
    save_dir,
    save_name,
    encoder,
    decoder,
    predictor,
    optimizer1,
    optimizer2
  )
  # Initialize tensorboard a.k.a. greatest thing since sliced bread
  writer = SummaryWriter(tensorboard_log_dir)
  for _ in range(100):
    for batch_idx, batch in enumerate(loader):
      batch_idx += batch_idx_offset

      if batch_idx < vae_train_steps:  # First part: Training the VAE
        if batch_idx > 10000:
          mask_mult = 1.
        else:
          mask_mult = 10.

        strokes = batch['stroke'].float().to(device)
        optimizer1.zero_grad()

        z, mu, log_var = encoder(strokes)
        recon_batch = decoder(z)

        mse, mask = reconstruction_loss_function(recon_batch, strokes, mask_mult)
        kld = kl_loss_function(mu, log_var, kl_tolerance, z_size)

        loss = mse + kld
        loss.backward()
        optimizer1.step()

        writer.add_scalar('loss', loss, batch_idx)
        writer.add_scalar('kl_loss', kld, batch_idx)
        writer.add_scalar('mse_loss', mse, batch_idx)
        writer.add_scalar('mask_mult', mask_mult, batch_idx)

        if batch_idx % tensorboard_every_n_steps == 0:
          writer.add_images('img_in', strokes[:3], batch_idx)
          writer.add_images('img_out', recon_batch[:3], batch_idx)
          if mask is not None:
            writer.add_images('img_mask', mask[:3], batch_idx)
        if batch_idx % log_every_n_steps == 0:
          print('train batch {}\tLoss: {:.6f}\tKLD: {:.6f}\tRLOSS: {:.6f}'.format(
            batch_idx, loss.item(), kld.item(), mse.item()
          ))

      else:  # Second part: Training the predictor
        # Hardcoded manual LR adjustments
        if batch_idx < vae_train_steps + 20000:
          for g in optimizer2.param_groups:
            g['lr'] = 0.01
        elif batch_idx < vae_train_steps + 80000:
          for g in optimizer2.param_groups:
            g['lr'] = 0.001
        else:
          for g in optimizer2.param_groups:
            g['lr'] = 0.0001

        strokes = batch['stroke'].float().to(device)
        actions = batch['action'].float().to(device)
        optimizer2.zero_grad()

        _, mu, log_var = encoder(strokes)
        predicted_z, predicted_mu, predicted_log_var = predictor(actions)

        mu_mse = F.mse_loss(mu, predicted_mu)
        log_var_mse = F.mse_loss(log_var, predicted_log_var)
        loss = mu_mse + log_var_mse
        loss.backward()
        optimizer2.step()

        writer.add_scalar('loss2', loss, batch_idx)
        writer.add_scalar('mu_mse', mu_mse, batch_idx)
        writer.add_scalar('log_var_mse', log_var_mse, batch_idx)

        if batch_idx % tensorboard_every_n_steps == 0:
          writer.add_images('img_in', strokes[:3], batch_idx)
          recon_batch = decoder(predicted_z)
          writer.add_images('img_out', recon_batch[:3], batch_idx)
        if batch_idx % log_every_n_steps == 0:
          print('train batch {}\tLoss: {:.6f}\tMU_MSE: {:.6f}\tLV_MSE: {:.6f}'.format(
            batch_idx, loss.item(), mu_mse.item(), log_var_mse.item()
          ))

      if batch_idx % save_every_n_steps == 0:
        save_train_checkpoint(save_dir, save_name, batch_idx,
                              encoder, decoder, predictor,
                              optimizer1, optimizer2)

    batch_idx_offset = batch_idx + 1

  writer.close()
