# Copyright 2018 The Hypebot Authors. All rights reserved.
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
"""esports_lib fetches LCS and other professional tournament data from Riot."""

# pylint: disable=broad-except

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import Counter
from collections import defaultdict
import copy
import random
from threading import RLock

from absl import logging
import arrow

from hypebot.core import name_complete_lib
from hypebot.core import util_lib
from hypebot.data.league import messages
from hypebot.data.league import nicknames
from hypebot.protos import esports_pb2

LIVESTREAM_LINK_FORMAT = 'https://gaming.youtube.com/embed/%s'


class Match(object):
  """Wraps protobuf match with business logic."""

  def __init__(self, match):
    self._match = match
    self.time = arrow.get(self._match.timestamp)
    # Matches in the past are assumed to have already been announced.
    self.announced = arrow.utcnow() > self.time

  def __repr__(self):
    return self._match.__repr__()

  def __str__(self):
    return str(self._match)

  def __getattr__(self, attr):
    return getattr(self._match, attr)


class TournamentProvider(object):
  """Provides common interface to a professional tournament."""

  def __init__(self, stats_enabled=False):
    self.stats_enabled = stats_enabled

  @property
  def league_id(self):
    """Unique abbreviation for league."""
    pass

  @property
  def name(self):
    """Human readable name of league."""
    pass

  @property
  def aliases(self):
    """List of alternate names for the league."""
    return []

  @property
  def teams(self):
    """List of teams participating in tournament."""
    return []

  @property
  def brackets(self):
    """List of brackets in the tournament.

    A tournament may be split into multiple brackets/rounds. E.g., a simple
    tournament may consist of a regular season followed by playoffs. Or a
    complex tournament can have play-in rounds, multiple pools, and a playoffs.

    Returns:
      List of brackets.
    """
    return []

  def _MakeBracketId(self, bracket_name):
    """Make bracket id such that it is globally unique."""
    return '%s-%s' % (self.league_id, bracket_name)

  def LoadData(self):
    """(Re)loads all data associated with the tournament."""
    raise NotImplementedError('TournamentProviders must be able to LoadData.')

  def UpdateMatches(self):
    """Poll for match updates.

    Returns a list of matches which have changed since the last LoadData.
    It may be smarter to allow users of the provider to register a callback to
    get "push" notifications for match updates, but that would require more
    changes to hypebot.
    """
    raise NotImplementedError(
        'TournamentProviders must be able to UpdateMatches.')


class RitoProvider(TournamentProvider):
  """Provides data for Rito tournaments.

  Scrapes lolesports.com undocumented APIs.
  """

  _POSITIONS = {
      'toplane': 'Top',
      'jungle': 'Jungle',
      'midlane': 'Mid',
      'adcarry': 'ADC',
      'support': 'Support',
  }

  def __init__(self, proxy, region, league, aliases=None, **kwargs):
    super(RitoProvider, self).__init__(**kwargs)
    self._proxy = proxy
    self._region = region
    self._league = league
    self._aliases = aliases or []

    self._lock = RLock()
    self._teams = {}
    self._brackets = {}
    self._matches = {}
    self._rosters = {}

  @property
  def league_id(self):
    return self._region

  @property
  def name(self):
    return self._region

  @property
  def aliases(self):
    return self._aliases

  @property
  def brackets(self):
    with self._lock:
      return self._brackets.values()

  @property
  def teams(self):
    with self._lock:
      return self._teams.values()

  def _FetchEsportsData(self,
                        api_endpoint,
                        api_id=None,
                        force_lookup=False,
                        use_storage=False):
    """Gets eSports data from rito, bypassing the cache if force_lookup."""
    if api_endpoint != 'streamgroups' and not api_id:
      return
    base_esports_url = 'http://api.lolesports.com/api/'
    endpoint_urls = {
        'leagues': 'v1/leagues?slug=%s',
        'matchDetails': 'v2/highlanderMatchDetails?tournamentId=%s&matchId=%s',
        'scheduleItems': 'v1/scheduleItems?id=%s',
        'streamgroups': 'v2/streamgroups%s',
        'teams': 'v1/teams?slug=%s&tournament=%s'
    }
    full_url = base_esports_url + endpoint_urls[api_endpoint] % (api_id or '')
    return self._proxy.FetchJson(full_url,
                                 force_lookup=force_lookup,
                                 use_storage=use_storage)

  def LoadData(self):
    with self._lock:
      self._teams = {}
      self._matches = {}
      self._rosters = {}

      league_data = self._FetchEsportsData('leagues', self._league)
      if not league_data or 'leagues' not in league_data:
        return

      for team in league_data['teams']:
        # team['id'] is a number (sometimes a string) used by rito to identify
        # the team. We prefer to use acronyms since it's easier to bet on TSM
        # than 11.
        team_id = str(team['id'])
        self._teams[team_id] = esports_pb2.Team(
            team_id=team['acronym'],
            name=team['name'],
            league_id=self.league_id)

      for tournament in self._FindActiveTournaments(
          league_data['highlanderTournaments']):
        logging.info('Pulling esports data from %s for %s/%s',
                     tournament['title'], self._region, self._league)
        for roster_id, roster in tournament['rosters'].items():
          if 'team' in roster:
            t = self._teams[roster['team']]
            self._rosters[roster_id] = t
            team = ([x for x in league_data['teams']
                     if str(x['id']) == roster['team']] or [None])[0]
            if t.players or not team:
              continue
            team_data = self._FetchEsportsData(
                'teams', (team['slug'], tournament['id']), use_storage=True)
            if not team_data or 'players' not in team_data:  # pylint: disable=unsupported-membership-test
              continue
            for player in team_data['players']:
              t.players.add(
                  summoner_name=player['name'],
                  team_id=t.team_id,
                  position=self._POSITIONS.get(player['roleSlug'], 'Feed'),
                  is_substitute=player['id'] in team['subs'])
          else:
            # It's a player at All-Stars.
            self._rosters[roster_id] = esports_pb2.Team(
                team_id=roster['name'],
                name=roster['name'])

        for bracket in tournament['brackets'].values():
          def _ExtractStandings(record, rank=None):
            return esports_pb2.TeamStanding(
                rank=rank,
                team=self._rosters[record['roster']],
                wins=record.get('wins', 0),
                losses=record.get('losses', 0),
                ties=record.get('ties', 0),
                points=record.get('score', 0))

          b = esports_pb2.Bracket(
              bracket_id=self._MakeBracketId(bracket['name']),
              name=bracket['name'].replace('_', ' ').title(),
              league_id=self.league_id,
              is_playoffs='playoff' in bracket['name'].lower())
          self._brackets[b.bracket_id] = b
          if 'standings' in bracket:
            for group in bracket['standings']['result']:
              rank = len(b.standings) + 1
              for roster in group:
                record = ([
                    r for r in league_data['highlanderRecords']
                    if r['tournament'] == tournament['id'] and r['bracket'] ==
                    bracket['id'] and r['roster'] == roster['roster']
                ] or [None])[0]
                if record:
                  b.standings.extend([_ExtractStandings(record, rank)])
          else:
            b.standings.extend([
                _ExtractStandings(r) for r in league_data['highlanderRecords']
                if (r['tournament'] == tournament['id'] and r['bracket'] ==
                    bracket['id'])
            ])

          for match in bracket['matches'].values():
            match_teams = self._ExtractMatchTeams(match)
            if not match_teams:
              continue
            m = b.schedule.add(
                match_id=match['id'],
                bracket_id=b.bracket_id,
                blue=match_teams[0],
                red=match_teams[1])
            self._matches[m.match_id] = m

            for game in match['games'].values():
              if 'id' not in game:
                continue
              # Rito has 2 different ids for the game, we temporarily store the
              # one needed to find the hash as the hash field.
              m.games.add(game_id=game.get('gameId'),
                          realm=game.get('gameRealm'),
                          hash=game['id'])

            if 'standings' in match:
              top_group = match['standings']['result'][0]
              if len(top_group) > 1:
                m.winner = 'TIE'
              elif len(top_group) == 1:
                m.winner = self._rosters[top_group[0]['roster']].team_id

              game_id_mappings = self._FetchEsportsData(
                  'matchDetails', (tournament['id'], m.match_id),
                  use_storage=True).get('gameIdMappings')
              for game in m.games:
                game_id = game.hash
                game.ClearField('hash')
                for mapping in game_id_mappings:
                  if mapping['id'] == game_id:
                    game.hash = mapping['gameHash']

          # Go back and fetch match time, because rito :^).
          schedule_data = self._FetchEsportsData(
              'scheduleItems', league_data['leagues'][0]['id'])
          if not schedule_data or 'scheduleItems' not in schedule_data:
            continue
          for match in schedule_data['scheduleItems']:
            match_id = match.get('match', 0)
            if match_id in self._matches:
              self._matches[match_id].timestamp = arrow.get(
                  match['scheduledTime']).timestamp

  def UpdateMatches(self):
    updated_matches = []
    with self._lock:
      league_data = self._FetchEsportsData('leagues', self._league,
                                           force_lookup=True)
      if not league_data or 'leagues' not in league_data:
        return []

      for tournament in self._FindActiveTournaments(
          league_data['highlanderTournaments']):
        for bracket in tournament['brackets'].values():
          for match in bracket['matches'].values():
            old_match = self._matches.get(match['id'])
            if not old_match:
              continue
            # Try to update 'TBD' teams in the match.
            if 'TBD' in (old_match.blue, old_match.red):
              match_teams = self._ExtractMatchTeams(match)
              if match_teams:
                old_match.blue = match_teams[0]
                old_match.red = match_teams[1]

            if not old_match.winner and 'standings' in match:
              top_group = match['standings']['result'][0]
              if len(top_group) > 1:
                old_match.winner = 'TIE'
              elif len(top_group) == 1:
                old_match.winner = self._rosters[top_group[0]['roster']].team_id
              updated_matches.append(old_match)
    return updated_matches

  def _FindActiveTournaments(self, tournaments):
    """From a list of highlanderTournaments, finds all active or most recent."""
    active_tournaments = []
    tournament = None
    newest_start_date = arrow.Arrow.min
    t_now = arrow.utcnow()
    for t in tournaments:
      if 'startDate' not in t:
        continue
      t_start_date = arrow.get(t['startDate'])
      t_end_date = arrow.get(t['endDate'])
      if t_start_date > newest_start_date:
        newest_start_date = t_start_date
        tournament = t
      if t_start_date <= t_now <= t_end_date:
        active_tournaments.append(t)
    return active_tournaments or [tournament]

  def _ExtractMatchTeams(self, match):
    """Returns a (red_team, blue_team) tuple of acroynms for teams in match."""
    match_teams = match.get('input', [])
    if len(match_teams) != 2:
      return

    def _FindTeam(team):
      if 'roster' in team:
        return self._rosters[team['roster']].team_id
      return 'TBD'

    match_teams = [_FindTeam(t) for t in match_teams]
    return match_teams


class GrumbleProvider(TournamentProvider):
  """Provide tournament informationg for a Grumble division."""

  _BASE_URL = (
      'http://goog-lol-tournaments.appspot.com/rest/%s/grumble-2018/')

  def __init__(self, proxy, division='D1', realm='NA1', **kwargs):
    super(GrumbleProvider, self).__init__(**kwargs)
    self._proxy = proxy
    self._division = division
    self._realm = realm
    self._teams = {}
    self._brackets = {}
    self._matches = {}

    self._lock = RLock()

  @property
  def league_id(self):
    return 'grumble-%s' % self._division

  @property
  def name(self):
    return 'Draft' if self._division == 'D1' else 'Open'

  @property
  def aliases(self):
    return [self._division]

  @property
  def brackets(self):
    with self._lock:
      return self._brackets.values()

  @property
  def teams(self):
    with self._lock:
      return self._teams.values()

  def _ParseSchedule(self, schedule, bracket):
    """Parse schedule into bracket."""
    match_count = 0
    standings = {}
    with self._lock:
      for week in schedule:
        for match in week['matches']:
          match_count += 1
          m = bracket.schedule.add(
              match_id='%s-%s-%s' % (
                  self.league_id, bracket.bracket_id, match_count),
              bracket_id=bracket.bracket_id,
              blue=util_lib.Access(match, 'team1.ref.id', 'TBD'),
              red=util_lib.Access(match, 'team2.ref.id', 'TBD'),
              timestamp=match['timestampSec'])
          self._matches[m.match_id] = m
          for game in match['games']:
            m.games.add(game_id=str(util_lib.Access(game, 'ref.gameId')),
                        realm=self._realm,
                        hash=util_lib.Access(game, 'ref.tournamentCode'))

          for team in [match['team1'], match['team2']]:
            team_id = util_lib.Access(team, 'ref.id')
            if not team_id:
              continue
            if team_id not in self._teams:
              self._teams[team_id] = esports_pb2.Team(
                  team_id=team_id,
                  name=team['ref']['displayName'],
                  league_id=self.league_id)
            if team_id not in standings:
              standings[team_id] = esports_pb2.TeamStanding(
                  team=self._teams[team_id])
            if not team['outcome']:
              continue
            if team['outcome'] == 'VICTORY':
              m.winner = team_id
              standings[team_id].wins += 1
              standings[team_id].points += 3
            elif team['outcome'] == 'TIE':
              m.winner = 'TIE'
              standings[team_id].ties += 1
              standings[team_id].points += 1
            else:
              standings[team_id].losses += 1
      standings = sorted(standings.values(), key=lambda x: x.points,
                         reverse=True)
      rank = 1
      cur_points = -1
      for i, team in enumerate(standings):
        if team.points != cur_points:
          rank = i + 1
          cur_points = team.points
        team.rank = rank
      bracket.standings.extend(standings)

  def LoadData(self):
    """Scrape goog-lol-tournament REST API for tournament data."""
    with self._lock:
      self._teams = {}
      self._brackets = {}
      self._matches = {}

      self._brackets['season'] = esports_pb2.Bracket(
          bracket_id=self._MakeBracketId('season'),
          name='Regular Season',
          league_id=self.league_id)
      response = self._proxy.FetchJson(
          '/'.join([self._BASE_URL % 'bracket', self._division, 'season']),
          force_lookup=True)
      self._ParseSchedule(response['schedule'], self._brackets['season'])

      self._brackets['playoffs'] = esports_pb2.Bracket(
          bracket_id=self._MakeBracketId('playoffs'),
          name='Playoffs',
          is_playoffs=True,
          league_id=self.league_id)
      response = self._proxy.FetchJson(
          '/'.join([self._BASE_URL % 'bracket', self._division, 'playoffs']),
          force_lookup=True)
      self._ParseSchedule(response['schedule'], self._brackets['playoffs'])

      for team_id, team in self._teams.items():
        response = self._proxy.FetchJson(
            '/'.join([self._BASE_URL % 'team', self._division, team_id]))
        if not response:
          continue
        for player in response['players']:
          team.players.add(
              summoner_name=player['summonerName'],
              team_id=team_id,
              position=random.choice(['Fill', 'Feed']))

  def _UpdateSchedule(self, schedule, bracket):
    """Update existing matches if they are now wonnered."""
    updated_matches = []
    match_count = 0
    with self._lock:
      for week in schedule:
        for match in week['matches']:
          match_count += 1
          match_id = '%s-%s-%s' % (
              self.league_id, bracket.bracket_id, match_count)
          old_match = self._matches.get(match_id)
          if not old_match or old_match.winner:
            continue
          for team in [match['team1'], match['team2']]:
            team_id = util_lib.Access(team, 'ref.id')
            if not team_id or not team['outcome']:
              continue
            if team['outcome'] == 'VICTORY':
              old_match.winner = team_id
            elif team['outcome'] == 'TIE':
              old_match.winner = 'TIE'
          if old_match.winner:
            updated_matches.append(old_match)
    return updated_matches

  def UpdateMatches(self):
    updated_matches = []
    with self._lock:
      response = self._proxy.FetchJson(
          '/'.join([self._BASE_URL % 'bracket', self._division, 'season']),
          force_lookup=True)
      updated_matches.extend(
          self._UpdateSchedule(response['schedule'], self._brackets['season']))

      response = self._proxy.FetchJson(
          '/'.join([self._BASE_URL % 'bracket', self._division, 'playoffs']),
          force_lookup=True)
      updated_matches.extend(
          self._UpdateSchedule(response['schedule'],
                               self._brackets['playoffs']))
    return updated_matches


class EsportsLib(object):
  """Electronic Sports Library."""

  def __init__(self, proxy, executor, game_lib, rito_tz):
    self._proxy = proxy
    self._executor = executor
    self._game = game_lib
    self._timezone = rito_tz

    self._providers = [
        GrumbleProvider(self._proxy, 'D1', stats_enabled=True),
        GrumbleProvider(self._proxy, 'D2', stats_enabled=True),
        RitoProvider(self._proxy, 'NA', 'na-lcs', aliases=['North America'],
                     stats_enabled=True),
        RitoProvider(self._proxy, 'EU', 'lec', aliases=['Europe'],
                     stats_enabled=True),
        # China is broken right now.
        # RitoProvider(self._proxy,
        #              'CN', 'lpl-china', aliases=['LPL', 'China']),
        RitoProvider(self._proxy, 'LCK', 'lck', aliases=['LCK', 'Korea', 'KR'],
                     stats_enabled=True),
        RitoProvider(self._proxy, 'IN', 'worlds',
                     aliases=['International', 'Worlds'],
                     stats_enabled=True),
    ]

    self._lock = RLock()
    self._teams = {}
    self._schedule = []
    self._matches = {}
    self._brackets = {}
    self._leagues = {}

    # Maintains mappings from champ_key -> per-region stats about picks, etc.
    self._champ_stats = defaultdict(lambda: defaultdict(Counter))
    # Maintains mappings from player_key -> various stats about the player like
    # per-champion wins/picks/etc.
    self._player_stats = defaultdict(lambda: defaultdict(Counter))
    # Maintains total number of games played per region
    self._num_games = Counter()

    self._callbacks = []

    # Load eSports data in the background on startup so we don't have to wait
    # for years while we fetch the bio of every player in multiple languages
    # once for every API call we make regardless of endpoint.
    self._load_status = self._executor.submit(self.LoadEsports)

  @property
  def teams(self):
    """Dictionary of [team id, name] => team."""
    with self._lock:
      return self._teams

  @property
  def schedule(self):
    """Time ordered list of matches."""
    with self._lock:
      return self._schedule

  @property
  def matches(self):
    """Dictionary of match id => match."""
    with self._lock:
      return self._matches

  @property
  def brackets(self):
    """Dictionary of bracket id => bracket."""
    with self._lock:
      return self._brackets

  @property
  def leagues(self):
    """Dictionary of [league id, alias, region] => bracket."""
    with self._lock:
      return self._leagues

  def RegisterCallback(self, fn):
    """Register a function to be called whenever the esports data is updated."""
    self._callbacks.append(fn)

  def IsReady(self):
    """Returns if all the dependant data for EsportsLib has been loaded."""
    return self._load_status.done()

  def ReloadData(self):
    self._proxy.FlushCache()
    self.LoadEsports()

  def LoadEsports(self):
    """Loads "static" data about each league."""
    # Reloading providers and aggregating data is slow. So we use temporary
    # variables and operate outside of the lock to allow other esports commands
    # to resolve.
    # TODO: Reload each provider in it's own threadpool.
    for provider in self._providers:
      provider.LoadData()

    teams = {}
    matches = {}
    brackets = {}
    leagues = {}
    champ_stats = defaultdict(lambda: defaultdict(Counter))
    player_stats = defaultdict(lambda: defaultdict(Counter))
    num_games = Counter()

    for league in self._providers:
      leagues[league.league_id] = league
      for bracket in league.brackets:
        brackets[bracket.bracket_id] = bracket
        for match in bracket.schedule:
          match = Match(match)
          matches[match.match_id] = match
          if match.winner and league.stats_enabled:
            self._ScrapePickBanData(
                league, match, champ_stats, player_stats, num_games)
      for team in league.teams:
        teams[team.team_id] = team

    with self._lock:
      self._teams = name_complete_lib.NameComplete(
          {team.name: team_id for team_id, team in teams.items()},
          teams)
      self._schedule = sorted(matches.values(), key=lambda x: x.timestamp)
      self._matches = matches
      self._brackets = brackets
      league_aliases = {
          league.name: league.league_id for league in leagues.values()
      }
      for league in leagues.values():
        for alias in league.aliases:
          league_aliases[alias] = league.league_id
      self._leagues = name_complete_lib.NameComplete(
          league_aliases, leagues)

      self._champ_stats = champ_stats
      self._player_stats = name_complete_lib.NameComplete(
          nicknames.LCS_PLAYER_NICKNAME_MAP,
          player_stats,
          [x['name'] for x in player_stats.values()])
      self._num_games = num_games

    logging.info('Loading esports complete, running callbacks.')
    for fn in self._callbacks:
      fn()
    logging.info('Esports callbacks complete.')

  def UpdateEsportsMatches(self):
    """Determines if any matches have been wonnered and returns them."""
    updated_matches = []
    for provider in self._providers:
      updated_matches.extend(provider.UpdateMatches())
    return updated_matches

  def GetLivestreamLinks(self):
    """Get links to the livestream(s), if any are currently active.

    Returns:
      Dict of match_id to link for livestreams.
    """
    # TODO: Determine how to handle livestream links. Rito provides a
    # single call to fetch all stream links regardless of the tournament, and
    # grumble does not currently provide links.
    return {}

  def GetSchedule(self, subcommand, include_playoffs, num_games=5):
    """Get the schedule for the specified region or team."""
    qualifier = 'All'
    with self._lock:
      if subcommand in self.teams:
        qualifier = self.teams[subcommand].team_id
      if subcommand in self.leagues:
        qualifier = self.leagues[subcommand].league_id

    now = arrow.utcnow()

    schedule = []
    livestream_links = self.GetLivestreamLinks()
    for match in self.schedule:
      if self._MatchIsInteresting(match, qualifier, now, include_playoffs):
        # If the game is in the future, add a livestream link if one exists. If
        # the game is considered live, add either an existing livestream link or
        # the fallback link.
        if match.time > now:
          if match.time == arrow.Arrow.max:
            # This means rito hasn't scheduled this match yet
            date_time = 'TBD'
          else:
            local_time = match.time.to(self._timezone)
            date_time = local_time.strftime('%a %m/%d %I:%M%p %Z')
          if match.match_id in livestream_links:
            date_time += ' - %s' % livestream_links[match.match_id]
        else:
          date_time = 'LIVE - ' + (livestream_links.get(match.match_id) or
                                   messages.FALLBACK_LIVESTREAM_LINK)
        num_games_str = ''
        if match.games:
          num_games_str = 'Bo%s - ' % len(match.games)
        blue_team = util_lib.Colorize('{:3}'.format(match.blue), 'blue')
        red_team = util_lib.Colorize('{:3}'.format(match.red), 'red')
        schedule.append('{} v {}: {}{}'.format(blue_team, red_team,
                                               num_games_str, date_time))
        if len(schedule) >= num_games:
          break

    if not schedule:
      schedule = [messages.SCHEDULE_NO_GAMES_STRING]
      qualifier = 'No'
    return schedule[:num_games], qualifier

  def GetResults(self, subcommand, num_games=5):
    """Get the results of past games for the specified region or team."""
    qualifier = 'All'
    is_team = False
    with self._lock:
      if subcommand in self.teams:
        qualifier = self.teams[subcommand].team_id
        is_team = True
      if subcommand in self.leagues:
        qualifier = self.leagues[subcommand].league_id
        is_team = False

    results = []
    # Shallow copy so we don't actually reverse the schedule
    tmp_schedule = self.schedule[:]
    tmp_schedule.reverse()
    for match in tmp_schedule:
      if self._ResultIsInteresting(match, qualifier):
        blue_team = util_lib.Colorize('{:3}'.format(match.blue), 'blue')
        red_team = util_lib.Colorize('{:3}'.format(match.red), 'red')
        is_tie = match.winner == 'TIE'
        if is_team:
          winner_msg = 'Tie' if is_tie else (
              'Won!' if match.winner == qualifier else 'Lost')
        else:
          winner_msg = '{} {}'.format(
              match.winner if is_tie else util_lib.Colorize(
                  '{:3}'.format(match.winner),
                  'red' if match.red == match.winner else 'blue'),
              ':^)' if is_tie else 'wins!')
        results.append('{} v {}: {}'.format(blue_team, red_team, winner_msg))
        if len(results) >= num_games:
          break

    return results[:num_games], qualifier

  def GetStandings(self, req_region, req_bracket):
    """Gets the standings for a specified region."""
    league = self.leagues[req_region]
    if not league:
      return []

    standings = []
    for bracket in league.brackets:
      if not (req_bracket in bracket.name or req_bracket in bracket.bracket_id):
        continue

      teams = sorted(bracket.standings, key=lambda x: x.rank)
      has_ties = any([team.ties for team in teams])
      standings.append(['%s-%s Standings (%s)' % (
          league.name, bracket.name,
          'W-L-D, Pts' if has_ties else 'W-L')])
      for team in teams:
        format_str = '{0.wins}-{0.losses}'
        if has_ties:
          format_str += '-{0.ties}, {0.points}'
        standings.append(
            ('{0.rank:2}: {0.team.team_id:3} (%s)' % format_str).format(team))

    return standings

  def GetChampPickBanRate(self, region, champ):
    """Returns pick/ban data for champ, optionally filtered by region."""
    champ_id = self._game.GetChampId(champ)
    with self._lock:
      if region in self.leagues:
        region = self.leagues[region].league_id
    if not champ_id:
      logging.info('%s doesn\'t map to any known champion', champ)
      return None, None
    canonical_name = self._game.GetChampDisplayName(champ)
    pick_ban_data = self._champ_stats[champ_id]

    summed_pick_ban_data = Counter()
    for region_data in [v for k, v in pick_ban_data.items() if
                        region in ('all', k)]:
      summed_pick_ban_data.update(region_data)
    summed_pick_ban_data['num_games'] = sum(
        v for k, v in self._num_games.items() if region in ('all', k))

    return canonical_name, summed_pick_ban_data

  def GetPlayerChampStats(self, query):
    """Returns champ statistics for LCS player."""
    player_info = self._player_stats[query]
    if not player_info:
      logging.info('%s is ambiguous or doesn\'t map to any known player',
                   query)
      return None, None

    player_data = {}
    player_data['champs'] = copy.deepcopy(player_info)
    player_data['num_games'] = copy.deepcopy(player_info['num_games'])
    # Remove non-champ keys from the champ data. This is also why we need the
    # deepcopies above.
    del player_data['champs']['name']
    del player_data['champs']['num_games']
    return player_info['name'], player_data

  def GetTopPickBanChamps(self, region, sort_key_fn, descending=True):
    """Returns the top 5 champions sorted by sort_key_fn for region."""
    with self._lock:
      if region in self.leagues:
        region = self.leagues[region].league_id
    champs_to_flattened_data = {}
    for champ_id, region_data in self._champ_stats.items():
      champ_name = self._game.GetChampNameFromId(champ_id)
      if not champ_name:
        logging.error('Couldn\'t parse %s into a champ_name', champ_id)
        continue
      champs_to_flattened_data[champ_name] = Counter()
      for r, pb_data in region_data.items():
        if region in ('all', r):
          champs_to_flattened_data[champ_name].update(pb_data)

    num_games = sum(v for k, v in self._num_games.items()
                    if region in ('all', k))

    # First filter out champs who were picked in fewer than 5% of games
    filtered_champs = [
        x for x in champs_to_flattened_data.items()
        if x[1]['picks'] >= (num_games / 20.0)
    ]
    # We sort by the secondary key (picks) first
    sorted_champs = sorted(
        filtered_champs, key=lambda x: x[1]['picks'], reverse=True)

    sorted_champs.sort(key=lambda x: sort_key_fn(x[1]), reverse=descending)

    logging.info('TopPickBanChamps in %s [key=%s, desc? %s] => (%s, %s)',
                 region, sort_key_fn, descending, num_games, sorted_champs[:5])
    return num_games, sorted_champs[:5]

  def GetUniqueChampCount(self, region):
    with self._lock:
      if region in self.leagues:
        region = self.leagues[region].league_id
    unique = set([self._game.GetChampNameFromId(k)
                  for k, c in self._champ_stats.items()
                  if region == 'all' or region in c.keys()])
    num_games = sum(v for k, v in self._num_games.items()
                    if region in ('all', k))
    return len(unique), num_games

  def _MatchIsInteresting(self, match, qualifier, now, include_playoffs):
    """Small helper method to check if a match is interesting right now."""
    bracket = self.brackets[match.bracket_id]
    region_and_teams = (bracket.league_id, match.blue, match.red)
    return ((qualifier == 'All' or qualifier in region_and_teams) and
            not match.winner and
            (match.time == arrow.Arrow.max or
             # Shift match time 1 hr/game into the future to show live matches.
             match.time.shift(hours=len(match.games)) > now) and
            (include_playoffs or not bracket.is_playoffs))

  def _ResultIsInteresting(self, match, qualifier):
    region_and_teams = (
        self.brackets[match.bracket_id].league_id, match.blue, match.red)
    if ((qualifier == 'All' or qualifier in region_and_teams) and match.winner):
      return True
    return False

  def _ScrapePickBanData(self,
                         league, match, champ_stats, player_stats, num_games):
    """For each game in match, fetches and tallies pick/ban data from Riot."""
    for game in match.games:
      if not game.hash:
        # In best-of series, not all games are played.
        continue
      num_games[league.league_id] += 1
      game_stats = self._proxy.FetchJson(
          'https://acs.leagueoflegends.com/v1/stats/game/%s/%s?gameHash=%s' %
          (game.realm, game.game_id, game.hash), use_storage=True)

      participant_to_player = {
          p['participantId']: util_lib.Access(p, 'player.summonerName')
          for p in game_stats.get('participantIdentities', [])
      }

      # Collect ban data.
      winning_team = None
      for team in game_stats.get('teams', []):
        if team.get('win') == 'Win':
          winning_team = team.get('teamId')
        for ban in team.get('bans', []):
          champ_stats[ban['championId']][league.league_id]['bans'] += 1

      # Collect pick and W/L data
      for player in game_stats.get('participants', []):
        champ_id = player['championId']
        champ_name = self._game.GetChampNameFromId(champ_id)

        # We need to use separate player_name and player_key here because Rito
        # doesn't like to keep things like capitalization consistent with player
        # names so they aren't useful as keys, but we still want to display the
        # "canonical" player name back to the user eventually, so we save it as
        # a value in player_stats instead.
        player_name = participant_to_player[player['participantId']]
        player_key = util_lib.CanonicalizeName(player_name or 'hypebot')
        player_stats[player_key]['name'] = player_name or 'HypeBot'
        if player.get('teamId') == winning_team:
          champ_stats[champ_id][league.league_id]['wins'] += 1
          player_stats[player_key][champ_name]['wins'] += 1
          player_stats[player_key]['num_games']['wins'] += 1
        champ_stats[champ_id][league.league_id]['picks'] += 1
        player_stats[player_key][champ_name]['picks'] += 1
        player_stats[player_key]['num_games']['picks'] += 1
