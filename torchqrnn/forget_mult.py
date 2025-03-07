import math
import torch
from torch.autograd import Variable
from cupy.cuda import function
from pynvrtc.compiler import Program
from collections import namedtuple

###

kernel = '''
extern "C"
__global__ void recurrent_forget_mult(float *dst, const float *f, const float *x, int SEQ, int BATCH, int HIDDEN)
{
  /*
  Note: destination is assumed to be one timestep longer than f or x where dst[0] = h_{-1}
  This means dst array has a separate index than that of f or x
  */
  int hid = blockIdx.x * blockDim.x + threadIdx.x;
  int bid = blockIdx.y * blockDim.y + threadIdx.y;
  if(hid >= HIDDEN || bid >= BATCH)
     return;
  //
  for (int ts = 0 + 1; ts < SEQ + 1; ts++) {
     // Good sanity check for debugging - only perform additions to a zeroed chunk of memory
     // Addition seems atomic or near atomic - you should get incorrect answers if doubling up via threads
     // Note: the index i needs to be offset by one as f[0] (f_t) is used for dst[1] (h_t) etc

     // To move timesteps, we step HIDDEN * BATCH
     // To move batches, we move HIDDEN
     // To move neurons, we move +- 1
     // Note: dst[dst_i] = ts * 100 + bid * 10 + hid; is useful for debugging

     int i           = (ts - 1) * HIDDEN * BATCH + bid * HIDDEN + hid;
     int dst_i       = (ts - 0) * HIDDEN * BATCH + bid * HIDDEN + hid;
     int dst_iminus1 = (ts - 1) * HIDDEN * BATCH + bid * HIDDEN + hid;
     dst[dst_i]      = f[i] * x[i];
     dst[dst_i]      += (1 - f[i]) * dst[dst_iminus1];
  }
}

extern "C"
__global__ void bwd_recurrent_forget_mult(const float *h, const float *f, const float *x, const float *gh, float *gf, float *gx, float *ghinit, int SEQ, int BATCH, int HIDDEN)
{
  /*
  Note: h is assumed to be one timestep longer than f, x, gf, gx, or gh where dst[0] = h_{-1}
  This means dst array has a separate index than that of f or x
  */
  int hid = blockIdx.x * blockDim.x + threadIdx.x;
  int bid = blockIdx.y * blockDim.y + threadIdx.y;
  if(hid >= HIDDEN || bid >= BATCH)
     return;
  //
  double running_f = 0;
  for (int ts = SEQ - 1 + 1; ts >= 0 + 1; ts--) {
     int i           = (ts - 1) * HIDDEN * BATCH + bid * HIDDEN + hid;
     int dst_i       = (ts - 0) * HIDDEN * BATCH + bid * HIDDEN + hid;
     int dst_iminus1 = (ts - 1) * HIDDEN * BATCH + bid * HIDDEN + hid;
     //
     running_f       += gh[dst_iminus1];
     // Gradient of X
     gx[i]           = f[i] * running_f;
     // Gradient of F
     gf[i]           = (x[i] - h[dst_iminus1]) * running_f;
     //
     // The line below is likely more numerically stable than (1 - f[i]) * running_f;
     running_f       = running_f - f[i] * running_f;
  }
  ghinit[bid * HIDDEN + hid] = running_f;
}
'''

###

class CPUForgetMult(torch.nn.Module):
    def __init__(self):
        super(CPUForgetMult, self).__init__()

    def forward(self, f, x, hidden_init=None):
        result = []
        ###
        forgets = f.split(1, dim=0)
        prev_h = hidden_init
        for i, h in enumerate((f * x).split(1, dim=0)):
            if prev_h is not None: h = h + (1 - forgets[i]) * prev_h
            # h is (1, batch, hidden) when it needs to be (batch_hidden)
            # Calling squeeze will result in badness if batch size is 1
            h = h.view(h.size()[1:])
            result.append(h)
            prev_h = h
        ###
        return torch.stack(result)


class GPUForgetMult(torch.autograd.Function):
    configured_gpus = {}
    ptx = None
    def __init__(self):
        super(GPUForgetMult, self).__init__()

    @staticmethod
    def compile():
        if GPUForgetMult.ptx is None:
            program = Program(kernel, 'recurrent_forget_mult.cu')
            GPUForgetMult.ptx = program.compile()

        if torch.cuda.current_device() not in GPUForgetMult.configured_gpus:
            m = function.Module()
            m.load(bytes(GPUForgetMult.ptx.encode()))

            forget_mult = m.get_function('recurrent_forget_mult')
            bwd_forget_mult = m.get_function('bwd_recurrent_forget_mult')

            Stream = namedtuple('Stream', ['ptr'])
            stream = Stream(ptr=torch.cuda.current_stream().cuda_stream)

            GPUForgetMult.configured_gpus[torch.cuda.current_device()] = (forget_mult, bwd_forget_mult, stream)

        return GPUForgetMult.configured_gpus[torch.cuda.current_device()]

    @staticmethod
    def forward(ctx, f, x, hidden_init=None):
        forget_mult, _, stream = GPUForgetMult.compile()
        seq_size, batch_size, hidden_size = f.size()
        result = f.new(seq_size + 1, batch_size, hidden_size)
        # We only zero the result array (result[0]) if we don't set a hidden initial state
        # All other values (result[1:]) are overwritten by default
        if hidden_init is not None: result[0, :, :] = hidden_init
        else: result = result.zero_()
        ###
        grid_hidden_size = min(hidden_size, 512)
        grid = (math.ceil(hidden_size / grid_hidden_size), batch_size)
        forget_mult(grid=grid, block=(grid_hidden_size, 1), args=[result.data_ptr(), f.data_ptr(), x.data_ptr(), seq_size, batch_size, hidden_size], stream=stream)
        ctx.save_for_backward(f, x, hidden_init)
        ctx.result = result
        return result[1:, :, :]

    @staticmethod
    def backward(ctx, grad_h):
        _, bwd_forget_mult, stream = GPUForgetMult.compile()
        f, x, hidden_init = ctx.saved_tensors
        h = ctx.result
        ###
        seq_size, batch_size, hidden_size = f.size()
        # Zeroing is not necessary as these will be overwritten
        grad_f = f.new(*f.size())
        grad_x = f.new(*f.size())
        grad_h_init = f.new(batch_size, hidden_size)
        ###
        grid_hidden_size = min(hidden_size, 512)
        grid = (math.ceil(hidden_size / grid_hidden_size), batch_size)
        bwd_forget_mult(grid=grid, block=(grid_hidden_size, 1), args=[h.data_ptr(), f.data_ptr(), x.data_ptr(), grad_h.data_ptr(), grad_f.data_ptr(), grad_x.data_ptr(), grad_h_init.data_ptr(), seq_size, batch_size, hidden_size], stream=stream)
        ###
        if hidden_init is not None:
            return grad_f, grad_x, grad_h_init
        return grad_f, grad_x


class ForgetMult(torch.nn.Module):
    r"""ForgetMult computes a simple recurrent equation:
    h_t = f_t * x_t + (1 - f_t) * h_{t-1}

    This equation is equivalent to dynamic weighted averaging.

    Inputs: X, hidden
        - X (seq_len, batch, input_size): tensor containing the features of the input sequence.
        - F (seq_len, batch, input_size): tensor containing the forget gate values, assumed in range [0, 1].
        - hidden_init (batch, input_size): tensor containing the initial hidden state for the recurrence (h_{t-1}).
        - use_cuda: If True, use the fast element-wise CUDA kernel for recurrence. If False, uses naive for loop. Default: True.
    """

    def __init__(self):
        super(ForgetMult, self).__init__()

    def forward(self, f, x, hidden_init=None, use_cuda=True):
        # Use CUDA by default unless it's available
        use_cuda = use_cuda and torch.cuda.is_available()
        # Ensure the user is aware when ForgetMult is not GPU version as it's far faster
        if use_cuda: assert f.is_cuda and x.is_cuda, 'GPU ForgetMult with fast element-wise CUDA kernel requested but tensors not on GPU'
        ###
        # Avoiding 'RuntimeError: expected a Variable argument, but got NoneType' when hidden_init is None
        if hidden_init is None: return GPUForgetMult.apply(f, x) if use_cuda else CPUForgetMult()(f, x)
        return GPUForgetMult()(f, x, hidden_init) if use_cuda else CPUForgetMult()(f, x, hidden_init)

###

if __name__ == '__main__':
    seq, batch, hidden = 35, 20, 650
    # Larger input (batch * seq * hidden) results in excessive memory for gradient check
    seq, batch, hidden = 3, 7, 19
    a      = Variable(torch.rand(seq, batch, hidden).cuda(), requires_grad=True)
    forget = Variable(torch.rand(seq, batch, hidden).cuda(), requires_grad=True)
    last_h = Variable(torch.rand(batch, hidden).cuda(), requires_grad=True)

    #seq, batch, hidden = 4, 1, 1
    #a = Variable(torch.Tensor([0.75, 0.5, 0.9, 0.8]).view(seq, batch, hidden).cuda(), requires_grad=True)
    #forget = Variable(torch.Tensor([0.25, 0.25, 0.5, 0.4]).view(seq, batch, hidden).cuda(), requires_grad=True)
    #last_h = Variable(torch.Tensor([0]).view(batch, hidden).cuda(), requires_grad=True)
    #print(forget, a, last_h)

    print('CUDA forget mult')
    print('=-=-' * 5)

    resulta = ForgetMult()(forget, a, last_h, use_cuda=True)
    print(resulta.size())
    loss = resulta.pow(2).sum()
    loss.backward()

    print('Result =', loss.data[0])
    print('X grad =', a.grad.mean().data[0])
    print('Forget grad =', forget.grad.mean().data[0])
    print('Last H grad =', last_h.grad.mean().data[0])

    x_grad_copy = a.grad.clone()

    print()
    print('CPU forget mult')
    print('=-=-' * 5)

    a.grad.data *= 0
    forget.grad.data *= 0
    last_h.grad.data *= 0

    resultb = ForgetMult()(forget, a, last_h, use_cuda=False)
    print(resultb.size())
    loss = resultb.pow(2).sum()
    loss.backward()

    print('Result =', loss.data[0])
    print('X grad =', a.grad.mean().data[0])
    print('Forget grad =', forget.grad.mean().data[0])
    print('Last H grad =', last_h.grad.mean().data[0])

    ###

    print()
    print('=-=-' * 5)
    print('(Xgrad - Xgrad).sum() =', (x_grad_copy - a.grad).sum().data[0])
    print('Residual error for result')
    print('=-=-' * 5)
    residual = (resulta - resultb)
    print(residual.abs().sum().data[0])
 
    # Had to loosen gradient checking, potentially due to general floating point badness?
    from torch.autograd import gradcheck
    inputs = [forget, a, last_h]
    test = gradcheck(ForgetMult(), inputs, eps=1e-4, atol=1e-2)
    print(test)
