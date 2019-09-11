# coding=utf-8
# Copyright 2018 The Hypebot Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""General utilities for use throughout hypebot code."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections
import copy
import datetime
import math
import random
import re
import threading
import unicodedata

import arrow
from dateutil.relativedelta import relativedelta
import six
from typing import Optional, Text

from hypebot.core import params_lib
from hypebot.protos.user_pb2 import User
from hypebot.protos.channel_pb2 import Channel


def Access(obj, path, default=None):
  """Tries to get a value from obj given by path, otherwise return default."""
  keys = path.split('.')
  for k in keys:
    try:
      obj = obj.get(k)
    except AttributeError:
      if obj is None:
        continue
      k = int(k)
      if k < len(obj):
        obj = obj[int(k)]
      else:
        obj = None
    if obj is None:
      return default
  return obj


def ArrowTime(hour=0, minute=0, second=0, tz='UTC', weekday=None):
  """Returns an Arrow object with the time portion set to a specific time."""
  time = arrow.now(tz).replace(
      hour=hour, minute=minute, second=second, microsecond=0)
  if weekday is not None:
    return arrow.Arrow.fromdatetime(time.datetime +
                                    relativedelta(weekday=weekday))
  return time


def BuildUser(user: Text, canonicalize: bool = True) -> User:
  """Returns a User proto for user, correctly setting user.id.

  Args:
    user: String representation of a user's identity.
    canonicalize: If the id for user should be canonicalized. This should only
      be False if we're building a User for the default user ("me"), where we
      don't want to allow users trying to impersonate a real user (e.g.
      brcooley_) to be able to act as that user. When in doubt, do not override
      the default value (True).

  Returns:
    User proto encapuslating the user.
  """
  if canonicalize:
    return User(name=user, id=CanonicalizeName(user))
  # This assumes that usernames are case-insensitive, e.g. foo == FoO.
  return User(name=user, id=user.strip().lower())


def CanonicalizeName(raw_name: Text):
  """Strips away all non-alphanumeric characters and converts to lowercase."""
  unicode_norm = unicodedata.normalize('NFKC', raw_name).lower()
  # We only match Ll (lowercase letters) since alphanumeric filtering is done
  # after converting to lowercase. Nl and Nd are numeric-like letters and
  # numeric digits.
  return ''.join(
      x for x in unicode_norm if unicodedata.category(x) in ('Ll', 'Nl', 'Nd'))


DECIMAL_POWERS = ('', 'k', 'm', 'b', 't', 'q', 'p')


def UnformatHypecoins(value):
  value = value.lower().rstrip()

  # Separate number and units.
  units = value.lstrip('0123456789eE-+.')
  if units:
    number = value[:-len(units)]
  else:
    number = value
  units = units.lstrip()
  try:
    number = int(number)
  except ValueError:
    number = float(number)

  scale = 0
  if units in DECIMAL_POWERS:
    scale = 1e3**DECIMAL_POWERS.index(units)
  if not scale:
    scale = 1
  return number * scale


def FormatHypecoins(amount, abbreviate=False):
  """Format hypecoins to a human readable amount.

  Args:
    amount: {int} amount of hypecoins to format.
    abbreviate: {boolean} whether to display full amount or convert large
      amounts to small strings. Abbreviate shows 3 sig figs using the k, M, G
      suffix system. Without abbreviate, just adds commas and ₡.

  Returns:
    {string} formatted hypecoin amount.
  """
  amount_str = 'NaN'
  if abbreviate:
    for power, prefix in enumerate(DECIMAL_POWERS):
      value = amount * 1e3**-power
      if abs(round(value)) < 1e3:
        break
    if prefix:
      # Round to at most 3 sigfigs.
      pattern = '%.{}f%s'.format(max(0, 2 - int(math.log10(round(abs(value))))))
      amount_str = pattern % (value, prefix)
    else:
      amount_str = '%d' % round(value)
  else:
    amount_str = '{:,d}'.format(amount)
  return '%s₡' % amount_str


def SafeCast(value, desired_type, default=None):
  """Cast between types without any pesky errors."""
  try:
    return desired_type(value)
  except (ValueError, TypeError):
    return default


def TimeDeltaToHumanDuration(time_delta, precision=1):
  """Converts a python timedelta object to a human-readable duration.

  Args:
    time_delta: Python timedelta object
    precision: Number of different units to display. The duration is converted
      into year, month, days, hours, minute, seconds.

  Returns:
    Human-readable string representing the duration of time_delta.
  """
  # A bunch of constants for funsies.
  # pylint: disable=invalid-name
  YEAR_IN_DAYS = 365.25  # Approximate.
  MONTH_IN_DAYS = YEAR_IN_DAYS / 12.0  # Average.
  HOUR_IN_SECONDS = 60 * 60.0
  MINUTE_IN_SECONDS = 60.0
  # pylint: enable=invalid-name

  parts = []
  if time_delta.days >= YEAR_IN_DAYS:
    parts.append('%dy' % (time_delta.days / YEAR_IN_DAYS))
    time_delta = datetime.timedelta(days=time_delta.days % YEAR_IN_DAYS)
  if time_delta.days >= 30:
    parts.append('%dmo' % (time_delta.days / MONTH_IN_DAYS))
    time_delta = datetime.timedelta(days=time_delta.days % MONTH_IN_DAYS)
  if time_delta.days >= 1:
    parts.append('%dd' % time_delta.days)
    time_delta = datetime.timedelta(seconds=time_delta.seconds)
  if time_delta.seconds >= HOUR_IN_SECONDS:
    parts.append('%dh' % (time_delta.seconds / HOUR_IN_SECONDS))
    time_delta = datetime.timedelta(seconds=time_delta.seconds %
                                    HOUR_IN_SECONDS)
  if time_delta.seconds >= MINUTE_IN_SECONDS:
    parts.append('%dm' % (time_delta.seconds / MINUTE_IN_SECONDS))
    time_delta = datetime.timedelta(seconds=time_delta.seconds %
                                    MINUTE_IN_SECONDS)
  parts.append('%dm' % time_delta.seconds)
  return ' '.join(parts[0:precision])


def SafeUrl(url, params=None):
  """Returns url with any sensitive information (API key) stripped."""
  if 'api_key' in url:
    url = ''.join((url.split('api_key')[0], '<redacted>'))
  if params:
    params = copy.copy(params)
    if 'api_key' in params:
      params['api_key'] = '<redacted>'
    url += ','.join(['%s=%s' % (k, v) for k, v in params.items()])
  return url


def GetWeightedChoice(options, prob_table, prob_table_lock=None):
  """Returns an option given its probability table, adjusting it for the future.

  The probability table is adjusted by spreading the current option's
  probability evenly across the other unfortunate options (including itself)
  that haven't had an opportunity to be picked.

  Args:
    options: List of options from where to pick a value.
    prob_table: Probability table for the options. It is a list where the
      element at index `i` has the probability for `options[i]`.
    prob_table_lock: (optional) The probability table may be accessed by several
      threads, in which case a lock may be provided.

  Returns:
    The selected option.
  """
  if prob_table_lock:
    with prob_table_lock:
      return _GetWeightedChoice(options, prob_table)
  else:
    return _GetWeightedChoice(options, prob_table)


def _GetWeightedChoice(options, prob_table):
  """Gets weighted choice without worrying about a lock."""
  # Initialize if this is not initialized
  if not prob_table:
    prob_table.extend([1.0 / len(options)] * len(options))

  # Get a random number and find which option matches such probability.
  r, p = random.random(), 0.0
  for i, opt_prob in enumerate(prob_table):
    p += opt_prob
    if p > r:
      break

  # pylint: disable=undefined-loop-variable
  # Mitigate rounding errors.
  option = options[i % len(options)]

  # Adjust the probability table.
  p, prob_table[i] = prob_table[i] / len(options), 0.0
  # pylint: enable=undefined-loop-variable
  for i, opt_prob in enumerate(prob_table):
    prob_table[i] = opt_prob + p

  return option


def Bold(string):
  """Returns string wrapped in escape codes representing bold typeface."""
  return '\x02%s\x0F' % string


def Italic(string):
  """Returns string wrapped in escape codes representing italic typeface."""
  return '\x1D%s\x0F' % string


def Underline(string):
  """Returns string wrapped in escape codes representing underlines."""
  return '\x1F%s\x0F' % string


_MIRC_COLORS = [
    'white', 'black', 'blue', 'green', 'red', 'brown', 'purple', 'orange',
    'yellow', 'light green', 'cyan', 'light cyan', 'light blue', 'pink', 'grey',
    'light grey'
]


def Colorize(string, color):
  """Returns string wrapped in escape codes representing color."""
  try:
    code = _MIRC_COLORS.index(color.lower())
    return '\x03%02d%s\x0f' % (code, string)
  except ValueError:
    return string


def StripColor(string):
  """Returns string with color escape codes removed."""
  regex = re.compile(r'\x03(?:\d{1,2}(?:,\d{1,2})?)?', re.UNICODE)
  return regex.sub('', string)


def Dankify(string):
  """Returns string with non-dank replaced by more dank ones."""
  return re.sub(r'([dD])(ark|usk)', r'\1ank', string)


def FuzzyBool(value):
  """Returns value as a boolean with special handling for false-like strings."""
  if (isinstance(value, six.string_types) and
      value.strip().lower() in ('false', 'no', '0')):
    return False
  return bool(value)


def Sparkline(values):
  """Returns an unicode sparkline representing values."""
  unicode_values = '▁▂▃▄▅▆▇█'
  if not values:
    return ''
  elif len(values) == 1:
    # Special case a single value to always return the middle value instead of
    # the smallest one, which would always be the case otherwise
    return unicode_values[len(unicode_values) // 2]
  min_value = min(values)
  # Really small offset used to ensure top bin includes max(values).
  value_range = max(values) - min_value + 1e-10
  bucket_size = value_range / len(unicode_values)
  bucketized_values = [int((v - min_value) / bucket_size) for v in values]
  return ''.join(unicode_values[v] for v in bucketized_values)


class UserTracker(object):
  """A class for tracking users (humans / bots) and their channels."""

  def __init__(self):
    self._bots = collections.defaultdict(set)  # name -> channels
    self._humans = collections.defaultdict(set)  # name -> channels
    self._lock = threading.Lock()

  def Reset(self):
    """Forget all tracked users."""
    with self._lock:
      self._bots.clear()
      self._humans.clear()

  def AddHuman(self, name, channel=None):
    """Adds name as a known human."""
    with self._lock:
      if channel:
        self._humans[name].add(channel.id)
      else:
        self._humans[name]  # pylint: disable=pointless-statement

  def AddBot(self, name, channel=None):
    """Adds name as a known bot."""
    with self._lock:
      if channel:
        self._bots[name].add(channel.id)
      else:
        self._bots[name]  # pylint: disable=pointless-statement

  def IsKnown(self, name):
    """Returns if name is known to this class, as either a bot or a human."""
    return self.IsHuman(name) or self.IsBot(name)

  def IsBot(self, name):
    """Returns if name is a bot."""
    return name in self._bots

  def IsHuman(self, name):
    """Returns if name is a human."""
    return name in self._humans

  def AllHumans(self, channel: Optional[Text] = None):
    """Returns a list of all users that are humans.

    Args:
      channel: If specified, only return humans in the channel.
    """
    if channel:
      return [
          name for name, channels in self._humans.items()
          if channel.id in channels
      ]
    return self._humans.keys()

  def AllBots(self, channel: Optional[Text] = None):
    """Returns a list of all users that are bots.

    Args:
      channel: If specified, only return bots in the channel.
    """
    if channel:
      return [
          name for name, channels in self._bots.items()
          if channel.id in channels
      ]
    return self._bots.keys()

  def AllUsers(self, channel: Optional[Text] = None):
    """Returns a list of all known users.

    Args:
      channel: If specified, only return users in the channel.
    """
    return self.AllHumans(channel) + self.AllBots(channel)


def MatchesAny(channels, channel):
  """Whether any channels' id is a prefix of channel.id."""
  for chan in channels:
    if channel.id.startswith(chan.id):
      return True
  return False
