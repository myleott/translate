#!/usr/bin/env python3

import argparse
import collections
import itertools
import os
import time
import torch

from fairseq import models


# Helper type for argparse to enable flippable boolean flags. For example,
# group.add_argument("--foo", type=utils.bool_flag, nargs="?", const=True,
#                    default=False)
# creates a --foo flag that defaults to False and can be set to True by
# specifying `--foo True` or just `--foo`. Some other --bar flag that defaults
# to True can be set to False by specifying `--bar False`.
def bool_flag(value):
    if value.lower() in ("true", "t", "1"):
        return True
    elif value.lower() in ("false", "f", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError(
            f"Expected boolean string such as 'true'/'false' instead of {value}."
        )


# The purpose of this class is to keep track of the list of checkpoints
# currently alive and automatically delete those that are no more required
# and that we do not want to keep around.
# In a nutshell, this class remembers the last max_num_checkpoints
# and delete (auto_clear == True) the oldest checkpoint each time a new one
# is added past this number.
class ManagedCheckpoints:

    # - max_num_checkpoints: Maximum number of checkpoints we need at one point.
    # - auto_clear: Control whether or not checkpoints should get deleted when
    #   they are not in the last max_num_checkpoints appended to the
    #   self anymore.
    def __init__(self, max_num_checkpoints, auto_clear):
        self.auto_clear = auto_clear
        assert max_num_checkpoints > 0, "Empty listing is not supported"
        self.kept_checkpoints = collections.deque(maxlen=max_num_checkpoints)

    def append(self, checkpoint_filename):
        # If we append a filename that we already manage, we would need
        # to remove it from its current position otherwise it may get deleted
        # by the time we reach the use for this append.
        # E.g., Let us assume we have a max of 2 checkpoint.
        # We insert last_checkpoint, use it, then insert last_checkpoint,
        # use it, then insert it again. The first file gets delete, but it
        # is actually the same as the current one, so we actually delete
        # the current one. Then we try to use it and we will get an error
        # for file not found.
        # Although this is pretty easy to support this case, given we only
        # append the same file names with no_epoch_checkpoints, we decided
        # not to slow every other uses case for that.
        # Instead we rely on the fact that when this happens, we actually
        # don't automatically delete files (auto_clear == False).
        assert not self.auto_clear or not self.kept_checkpoints.count(
            checkpoint_filename
        ), "Not yet implemented"
        if (
            self.auto_clear
            and len(self.kept_checkpoints) == self.kept_checkpoints.maxlen
        ):
            # We reach the max number of checkpoints we keep around.
            # Delete the oldest one.
            os.remove(self.kept_checkpoints.popleft())
        # Save the new checkpoint.
        self.kept_checkpoints.append(checkpoint_filename)

    def get_last_n(self, num_elements):
        assert 0 < num_elements <= self.kept_checkpoints.maxlen, (
            f"Requested number of elements {num_elements} "
            f"must be between 0 and maxlen {self.kept_checkpoints.maxlen}, "
            f"exclusive"
        )
        # If we ask for more elements than what we currently have, return all
        # of them.
        # Reason why we don't assert unlike for maxlen is because maxlen points
        # out a design issue (the reserved size is too small), whereas the case
        # where we ask more elements than what is currently in the list happens
        # when we print the average of X checkpoints for BLEU, but we haven't
        # yet computed that many checkpoints. We could also assert in this case
        # and fix the caller, but handling it here was just fine!
        start = max(len(self.kept_checkpoints) - num_elements, 0)
        return collections.deque(itertools.islice(self.kept_checkpoints, start, None))


# Variation on the fairseq StopwatchMeter that separates statistics by number
# of tokens. Sentences longer than max_length are stored in the last bucket.
class BucketStopwatchMeter(object):
    def __init__(self, increment, max_length, sentences_per_batch):
        self.increment = increment
        self.n_buckets = max_length // increment + 1
        self.sentences_per_batch = sentences_per_batch
        self.reset()

    def start(self):
        self.start_time = time.time()

    def stop(self, n=1):
        if self.start_time is not None:
            delta = time.time() - self.start_time
            bucket_id = min(
                self.n_buckets - 1,
                n // self.increment,
            )
            self.sum[bucket_id] += delta
            self.n[bucket_id] += n
            self.count[bucket_id] += 1
            self.start_time = None

    def reset(self):
        self.sum = [0] * self.n_buckets
        self.n = [0] * self.n_buckets
        self.count = [0] * self.n_buckets
        self.start_time = None

    def reset_bucket(self, bucket_id):
        if self.start_time is None:
            self.sum[bucket_id] = 0
            self.n[bucket_id] = 0
            self.count[bucket_id] = 0

    @property
    def avg(self):
        return sum(self.sum) / sum(self.n)

    @property
    def avgs(self):
        result = [0] * self.n_buckets
        for i in range(self.n_buckets):
            if self.n[i] != 0:
                result[i] = self.sum[i] / self.n[i]
            else:
                result[i] = 0
        return result


def load_diverse_ensemble_for_inference(filenames, src_dict, dst_dict):
    """Load an ensemble of diverse models for inference.

    This method is similar to fairseq.utils.load_ensemble_for_inference
    but allows to load diverse models with non-uniform args.

    Args:
        filenames: List of file names to checkpoints
        src_dict: Source dictionary
        dst_dict: Target dictionary

    Return:
        models, args: Tuple of lists. models contains the loaded models, args
        the corresponding configurations.
    """

    # load model architectures and weights
    states = []
    for filename in filenames:
        if not os.path.exists(filename):
            raise IOError("Model file not found: {}".format(filename))
        states.append(
            torch.load(
                filename,
                map_location=lambda s, l: torch.serialization.default_restore_location(
                    s, "cpu"
                ),
            )
        )
    # build ensemble
    ensemble = []
    for state in states:
        model = models.build_model(state["args"], src_dict, dst_dict)
        model.load_state_dict(state["model"])
        ensemble.append(model)
    return ensemble, [s["args"] for s in states]
