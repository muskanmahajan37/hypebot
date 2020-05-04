# Lint as: python3
# coding=utf-8
# Copyright 2020 The Hypebot Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Commands for interacting with HypeCoffee."""

from typing import Optional, Text

from grpc import StatusCode

from hypebot.commands import command_lib
from hypebot.core import inflect_lib
from hypebot.core import util_lib
from hypebot.protos import channel_pb2
from hypebot.protos import coffee_pb2
from hypebot.protos import message_pb2
from hypebot.protos import user_pb2

_RARITY_COLORS = {
    'uncommon': 'green',
    'rare': 'cyan',
    'precious': 'purple',
    'legendary': 'orange'
}

# Various messages extracted here for ease of testing
FOUND_NO_BEANS_MESSAGE = 'You couldn\'t find any coffee beans.'
BEAN_STASH_FULL_MESSAGE = ('You already have too many beans, try drinking some '
                           'coffee.')
OUT_OF_ENERGY_MESSAGE = ('You are too tired to look in your pantry, try '
                         'drinking some coffee.')
OUT_OF_COFFEE_MESSAGE = 'You don\'t have any coffee, try finding some.'
UNOWNED_BEAN_MESSAGE = 'You don\'t own any beans with ID "%s".'


def FormatBean(bean_data: coffee_pb2.Bean, uppercase: bool = False) -> Text:
  """Given a bean, returns a pretty string for output."""
  rarity = bean_data.rarity
  rarity_str = rarity
  if uppercase:
    rarity_str = rarity.title()
  if rarity in _RARITY_COLORS:
    rarity_str = util_lib.Colorize(
        rarity_str, _RARITY_COLORS[rarity], irc=False)
  return '%s %s beans from %s' % (rarity_str, bean_data.variety,
                                  bean_data.region)


@command_lib.CommandRegexParser(
    r'coffee (?:d(?:rink)?)(?:\s+(?P<bean_id>[A-Za-z0-9]+))?')
class DrinkCoffeeCommand(command_lib.BaseCommand):
  """Plebs run on Coffee."""

  def _Handle(self,
              channel: channel_pb2.Channel,
              user: user_pb2.User,
              bean_id: Optional[Text] = None):
    result = self._core.coffee.DrinkCoffee(user, bean_id)
    if result == StatusCode.NOT_FOUND:
      if bean_id:
        return UNOWNED_BEAN_MESSAGE % bean_id
      return OUT_OF_COFFEE_MESSAGE
    energy = result['energy']
    return '{}ou drank some coffee giving you {} energy.'.format(
        'Mmm, y' if energy > 3 else 'Y', energy)


@command_lib.CommandRegexParser(r'coffee f(?:ind)?')
class FindCoffeeCommand(command_lib.BaseCommand):
  """Let plebs try to find some coffee beans."""

  def _Handle(self, channel: channel_pb2.Channel, user: user_pb2.User):
    result = self._core.coffee.FindBeans(user)
    if result == StatusCode.RESOURCE_EXHAUSTED:
      return OUT_OF_ENERGY_MESSAGE
    if result == StatusCode.NOT_FOUND:
      return FOUND_NO_BEANS_MESSAGE
    if result == StatusCode.OUT_OF_RANGE:
      return BEAN_STASH_FULL_MESSAGE

    return ('You found some {} ({:.2%} chance)!').format(
        FormatBean(result), self._core.coffee.GetOccurrenceChance(result))


@command_lib.CommandRegexParser(r'coffee')
@command_lib.CommandRegexParser(
    r'coffee (?:s(?:tash)?)(?: (?P<target_user>.*))?')
class CoffeeStashCommand(command_lib.BaseCommand):
  """See yours or others' bean stashes."""

  def _Handle(self,
              channel: channel_pb2.Channel,
              user: user_pb2.User,
              target_user: Optional[user_pb2.User] = None):
    if not target_user:
      target_user = user
    coffee_data = self._core.coffee.GetCoffeeData(target_user)
    card = message_pb2.Card(
        header=message_pb2.Card.Header(
            title='%s\'s Coffee Stash:' % target_user.display_name,
            subtitle='%d energy | %s' %
            (coffee_data.energy,
             inflect_lib.Plural(len(coffee_data.beans or []), 'bean'))))
    beans = sorted(coffee_data.beans, key=self._core.coffee.GetOccurrenceChance)
    for bean in beans:
      card.fields.add(text=FormatBean(bean, uppercase=True))
    return card
