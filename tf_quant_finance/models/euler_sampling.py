# Lint as: python3
# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The Euler sampling method for ito processes."""

import tensorflow.compat.v2 as tf
from tf_quant_finance.math import random_ops as random
from tf_quant_finance.models import utils


def sample(dim,
           drift_fn,
           volatility_fn,
           times,
           time_step,
           num_samples=1,
           initial_state=None,
           random_type=None,
           seed=None,
           swap_memory=True,
           skip=0,
           dtype=None,
           name=None):
  """Returns a sample paths from the process using Euler method.

  For an Ito process,

  ```
    dX = a(t, X_t) dt + b(t, X_t) dW_t
  ```
  with given drift `a` and volatility `b` functions Euler method generates a
  sequence {X_n} as

  ```
  X_{n+1} = X_n + a(t_n, X_n) dt + b(t_n, X_n) (N(0, t_{n+1}) - N(0, t_n)),
  ```
  where `dt = t_{n+1} - t_n` and `N` is a sample from the Normal distribution.
  See [1] for details.

  ### References
  [1]: Wikipedia. Euler-Maruyama method:
  https://en.wikipedia.org/wiki/Euler-Maruyama_method

  Args:
    dim: Python int greater than or equal to 1. The dimension of the Ito
      Process.
    drift_fn: A Python callable to compute the drift of the process. The
      callable should accept two real `Tensor` arguments of the same dtype.
      The first argument is the scalar time t, the second argument is the
      value of Ito process X - tensor of shape `batch_shape + [dim]`.
      The result is value of drift a(t, X). The return value of the callable
      is a real `Tensor` of the same dtype as the input arguments and of shape
      `batch_shape + [dim]`.
    volatility_fn: A Python callable to compute the volatility of the process.
      The callable should accept two real `Tensor` arguments of the same dtype
      and shape `times_shape`. The first argument is the scalar time t, the
      second argument is the value of Ito process X - tensor of shape
      `batch_shape + [dim]`. The result is value of drift b(t, X). The return
      value of the callable is a real `Tensor` of the same dtype as the input
      arguments and of shape `batch_shape + [dim, dim]`.
    times: Rank 1 `Tensor` of increasing positive real values. The times at
      which the path points are to be evaluated.
    time_step: Scalar real `Tensor` - maximal distance between points
        in grid in Euler schema.
    num_samples: Positive scalar `int`. The number of paths to draw.
      Default value: 1.
    initial_state: `Tensor` of shape `[dim]`. The initial state of the
      process.
      Default value: None which maps to a zero initial state.
    random_type: Enum value of `RandomType`. The type of (quasi)-random
      number generator to use to generate the paths.
      Default value: None which maps to the standard pseudo-random numbers.
    seed: Python `int`. The random seed to use.
      Default value: None, which  means no seed is set.
    swap_memory: A Python bool. Whether GPU-CPU memory swap is enabled for this
      op. See an equivalent flag in `tf.while_loop` documentation for more
      details. Useful when computing a gradient of the op since `tf.while_loop`
      is used to propagate stochastic process in time.
      Default value: True.
    skip: `int32` 0-d `Tensor`. The number of initial points of the Sobol or
      Halton sequence to skip. Used only when `random_type` is 'SOBOL',
      'HALTON', or 'HALTON_RANDOMIZED', otherwise ignored.
      Default value: `0`.
    dtype: `tf.Dtype`. If supplied the dtype for the input and output `Tensor`s.
      Default value: None which means that the dtype implied by `times` is
      used.
    name: Python string. The name to give this op.
      Default value: `None` which maps to `euler_sample`.

  Returns:
   A real `Tensor` of shape [num_samples, k, n] where `k` is the size of the
      `times`, `n` is the dimension of the process.

  Raises:
    ValueError: If `time_step` or `times` have a non-constant value (e.g.,
      values are random), and `random_type` is `SOBOL`. This will be fixed with
      the release of TensorFlow 2.2.
  """
  name = name or 'euler_sample'
  with tf.name_scope(name):
    times = tf.convert_to_tensor(times, dtype=dtype)
    if dtype is None:
      dtype = times.dtype
    if initial_state is None:
      initial_state = tf.zeros(dim, dtype=dtype)
    initial_state = tf.convert_to_tensor(initial_state, dtype=dtype,
                                         name='initial_state')
    num_requested_times = times.shape.as_list()[0]
    # Create a time grid for the Euler scheme.
    times, keep_mask = _prepare_grid(
        times=times, time_step=time_step, dtype=dtype)
    return _sample(
        dim=dim,
        drift_fn=drift_fn,
        volatility_fn=volatility_fn,
        times=times,
        time_step=time_step,
        keep_mask=keep_mask,
        num_requested_times=num_requested_times,
        num_samples=num_samples,
        initial_state=initial_state,
        random_type=random_type,
        seed=seed,
        swap_memory=swap_memory,
        skip=skip,
        dtype=dtype)


def _sample(*, dim, drift_fn, volatility_fn, times, time_step, keep_mask,
            num_requested_times,
            num_samples, initial_state, random_type,
            seed, swap_memory, skip, dtype):
  """Returns a sample of paths from the process using Euler method."""
  dt = times[1:] - times[:-1]
  sqrt_dt = tf.sqrt(dt)
  current_state = initial_state + tf.zeros([num_samples, dim],
                                           dtype=initial_state.dtype)
  if dt.shape.is_fully_defined():
    steps_num = dt.shape.as_list()[-1]
  else:
    steps_num = tf.shape(dt)[-1]
    # TODO(b/148133811): Re-enable Sobol test when TF 2.2 is released.
    if random_type == random.RandomType.SOBOL:
      raise ValueError('Sobol sequence for Euler sampling is temporarily '
                       'unsupported when `time_step` or `times` have a '
                       'non-constant value')

  # In order to use low-discrepancy random_type we need to generate the sequence
  # of independent random normals upfront.
  if random_type in (random.RandomType.SOBOL,
                     random.RandomType.HALTON,
                     random.RandomType.HALTON_RANDOMIZED):
    normal_draws = utils.generate_mc_normal_draws(
        num_normal_draws=dim, num_time_steps=steps_num,
        num_sample_paths=num_samples, random_type=random_type,
        dtype=dtype, seed=seed, skip=skip)
    wiener_mean = None
  else:
    # If pseudo or anthithetic sampling is used, proceed with random sampling
    # at each step.
    wiener_mean = tf.zeros((dim,), dtype=dtype, name='wiener_mean')
    normal_draws = None
  cond_fn = lambda i, *args: i < steps_num
  # Maximum number iterations is passed to the while loop below. It improves
  # performance of the while loop on a GPU and is needed for XLA-compilation
  # comptatiblity.
  def step_fn(i, written_count, current_state, result):
    return _euler_step(
        i=i,
        written_count=written_count,
        current_state=current_state,
        result=result,
        drift_fn=drift_fn,
        volatility_fn=volatility_fn,
        wiener_mean=wiener_mean,
        num_samples=num_samples,
        times=times,
        dt=dt,
        sqrt_dt=sqrt_dt,
        keep_mask=keep_mask,
        random_type=random_type,
        seed=seed,
        normal_draws=normal_draws)

  maximum_iterations = (tf.cast(1. / time_step, dtype=tf.int32)
                        + tf.size(times))

  result = tf.zeros((num_samples, num_requested_times, dim), dtype=dtype)
  _, _, _, result = tf.while_loop(
      cond_fn, step_fn, (0, 0, current_state, result),
      maximum_iterations=maximum_iterations,
      swap_memory=swap_memory)
  return result


def _euler_step(*, i, written_count, current_state, result,
                drift_fn, volatility_fn, wiener_mean,
                num_samples, times, dt, sqrt_dt, keep_mask,
                random_type, seed, normal_draws):
  """Performs one step of Euler scheme."""
  current_time = times[i + 1]
  if normal_draws is not None:
    dw = normal_draws[i]
  else:
    dw = random.mv_normal_sample(
        (num_samples,), mean=wiener_mean, random_type=random_type,
        seed=seed)
  dw = dw * sqrt_dt[i]
  dt_inc = dt[i] * drift_fn(current_time, current_state)  # pylint: disable=not-callable
  dw_inc = tf.linalg.matvec(volatility_fn(current_time, current_state), dw)  # pylint: disable=not-callable
  next_state = current_state + dt_inc + dw_inc
  result = utils.maybe_update_along_axis(
      tensor=result,
      do_update=keep_mask[i + 1],
      ind=written_count,
      axis=1,
      new_tensor=tf.expand_dims(next_state, axis=1))
  written_count += tf.cast(keep_mask[i + 1], dtype=tf.int32)
  return i + 1, written_count, next_state, result


def _prepare_grid(*, times, time_step, dtype):
  """Prepares grid of times for path generation.

  Args:
    times:  Rank 1 `Tensor` of increasing positive real values. The times at
      which the path points are to be evaluated.
    time_step: Rank 0 real `Tensor`. Maximal distance between points in
      resulting grid.
    dtype: `tf.Dtype` of the input and output `Tensor`s.

  Returns:
    Tuple `(all_times, mask)`.
    `all_times` is a 1-D real `Tensor` containing all points from 'times` and
    the uniform grid of points between `[0, times[-1]]` with grid size equal to
    `time_step`. The `Tensor` is sorted in ascending order and may contain
    duplicates.
    `mask` is a boolean 1-D `Tensor` of the same shape as 'all_times', showing
    which elements of 'all_times' correspond to THE values from `times`.
    Guarantees that times[0]=0 and mask[0]=False.
  """
  grid = tf.range(0.0, times[-1], time_step, dtype=dtype)
  all_times = tf.concat([grid, times], axis=0)
  mask = tf.concat([
      tf.zeros_like(grid, dtype=tf.bool),
      tf.ones_like(times, dtype=tf.bool)
  ],
                   axis=0)
  perm = tf.argsort(all_times, stable=True)
  all_times = tf.gather(all_times, perm)
  mask = tf.gather(mask, perm)
  return all_times, mask
