"""Generate recorded-shape fixtures for parser and integrity tests.

These are hand-built rather than captured, because this environment had no
egress to any upstream (the gateway returned 403 to CONNECT for every data
host). They mirror the documented response shapes and, where a shape could not
be verified, the corresponding parser is config-driven so a correction is a YAML
edit rather than a code change.

The schedule fixture deliberately contains a doubleheader: two game_pks sharing
a date and both teams. It is the case that breaks naive keying, so every test
that touches joins should have to survive it.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).parent

NYY, BOS, LAD, SFG = 147, 111, 119, 137
YANKEE_STADIUM, FENWAY = 3313, 3

SCHEDULE = {
    "totalGames": 3,
    "dates": [
        {
            "date": "2025-04-01",
            "games": [
                {
                    "gamePk": 776001,
                    "season": "2025",
                    "gameType": "R",
                    "gameDate": "2025-04-01T17:05:00Z",
                    "officialDate": "2025-04-01",
                    "status": {"detailedState": "Final", "statusCode": "F"},
                    "teams": {
                        "home": {
                            "team": {"id": NYY, "name": "New York Yankees"},
                            "probablePitcher": {
                                "id": 543037,
                                "fullName": "Gerrit Cole",
                                "pitchHand": {"code": "R"},
                            },
                        },
                        "away": {
                            "team": {"id": BOS, "name": "Boston Red Sox"},
                            "probablePitcher": {
                                "id": 605483,
                                "fullName": "Brayan Bello",
                                "pitchHand": {"code": "R"},
                            },
                        },
                    },
                    "venue": {"id": YANKEE_STADIUM, "name": "Yankee Stadium"},
                    "doubleHeader": "S",
                    "gameNumber": 1,
                    "seriesGameNumber": 1,
                    "gamesInSeries": 3,
                    "dayNight": "day",
                    "scheduledInnings": 9,
                },
                {
                    # Same date, same teams, different game_pk. A
                    # (date, home, away) key merges this with the game above.
                    "gamePk": 776002,
                    "season": "2025",
                    "gameType": "R",
                    "gameDate": "2025-04-01T23:05:00Z",
                    "officialDate": "2025-04-01",
                    "status": {"detailedState": "Final", "statusCode": "F"},
                    "teams": {
                        "home": {
                            "team": {"id": NYY, "name": "New York Yankees"},
                            "probablePitcher": {
                                "id": 592332,
                                "fullName": "Carlos Rodon",
                                "pitchHand": {"code": "L"},
                            },
                        },
                        "away": {
                            "team": {"id": BOS, "name": "Boston Red Sox"},
                            "probablePitcher": {
                                "id": 656302,
                                "fullName": "Tanner Houck",
                                "pitchHand": {"code": "R"},
                            },
                        },
                    },
                    "venue": {"id": YANKEE_STADIUM, "name": "Yankee Stadium"},
                    "doubleHeader": "S",
                    "gameNumber": 2,
                    "seriesGameNumber": 2,
                    "gamesInSeries": 3,
                    "dayNight": "night",
                    "scheduledInnings": 9,
                },
                {
                    # A late Pacific first pitch: 02:10 UTC the NEXT day. Its
                    # slate date must stay 2025-04-01.
                    "gamePk": 776003,
                    "season": "2025",
                    "gameType": "R",
                    "gameDate": "2025-04-02T02:10:00Z",
                    "officialDate": "2025-04-01",
                    "status": {"detailedState": "Final", "statusCode": "F"},
                    "teams": {
                        "home": {
                            "team": {"id": LAD, "name": "Los Angeles Dodgers"},
                            "probablePitcher": {
                                "id": 477132,
                                "fullName": "Clayton Kershaw",
                                "pitchHand": {"code": "L"},
                            },
                        },
                        "away": {
                            "team": {"id": SFG, "name": "San Francisco Giants"},
                            "probablePitcher": {
                                "id": 664062,
                                "fullName": "Logan Webb",
                                "pitchHand": {"code": "R"},
                            },
                        },
                    },
                    "venue": {"id": 22, "name": "Dodger Stadium"},
                    "doubleHeader": "N",
                    "gameNumber": 1,
                    "seriesGameNumber": 1,
                    "gamesInSeries": 3,
                    "dayNight": "night",
                    "scheduledInnings": 9,
                },
            ],
        }
    ],
}


def _player(pid: int, name: str, position: str, bats: str = "R") -> dict:
    return {
        "person": {"id": pid, "fullName": name},
        "position": {"abbreviation": position},
        "batSide": {"code": bats},
    }


GAME_FEED = {
    "gamePk": 776001,
    "gameData": {
        "game": {"pk": 776001, "type": "R", "season": "2025"},
        "datetime": {"dateTime": "2025-04-01T17:05:00Z", "officialDate": "2025-04-01"},
        "status": {"detailedState": "Final", "statusCode": "F"},
        "players": {
            f"ID{600000 + i}": {
                "id": 600000 + i,
                "fullName": f"Home Batter {i}",
                "batSide": {"code": "R" if i % 2 else "L"},
            }
            for i in range(1, 10)
        }
        | {
            f"ID{700000 + i}": {
                "id": 700000 + i,
                "fullName": f"Away Batter {i}",
                "batSide": {"code": "L" if i % 2 else "R"},
            }
            for i in range(1, 10)
        },
    },
    "liveData": {
        "boxscore": {
            "teams": {
                "home": {
                    "team": {"id": NYY, "name": "New York Yankees"},
                    "battingOrder": [600000 + i for i in range(1, 10)],
                    "pitchers": [543037, 543038],
                    "players": {
                        f"ID{600000 + i}": _player(600000 + i, f"Home Batter {i}", "LF")
                        | {
                            "stats": {
                                "batting": {
                                    "plateAppearances": 4,
                                    "atBats": 4,
                                    "hits": 1,
                                    "doubles": 0,
                                    "triples": 0,
                                    "homeRuns": 1 if i == 3 else 0,
                                    "baseOnBalls": 0,
                                    "strikeOuts": 1,
                                    "runs": 1 if i == 3 else 0,
                                    "rbi": 1 if i == 3 else 0,
                                }
                            }
                        }
                        for i in range(1, 10)
                    }
                    | {
                        "ID543037": {
                            "person": {"id": 543037, "fullName": "Gerrit Cole"},
                            "position": {"abbreviation": "P"},
                            "stats": {
                                "pitching": {
                                    "inningsPitched": "6.2",
                                    "battersFaced": 26,
                                    "numberOfPitches": 98,
                                    "strikeOuts": 9,
                                    "baseOnBalls": 1,
                                    "hits": 4,
                                    "homeRuns": 1,
                                    "earnedRuns": 2,
                                }
                            },
                        },
                        "ID543038": {
                            "person": {"id": 543038, "fullName": "Relief Arm"},
                            "position": {"abbreviation": "P"},
                            "stats": {
                                "pitching": {
                                    "inningsPitched": "2.1",
                                    "battersFaced": 8,
                                    "numberOfPitches": 31,
                                    "strikeOuts": 3,
                                    "baseOnBalls": 0,
                                    "hits": 1,
                                    "homeRuns": 0,
                                    "earnedRuns": 0,
                                }
                            },
                        },
                    },
                },
                "away": {
                    "team": {"id": BOS, "name": "Boston Red Sox"},
                    "battingOrder": [700000 + i for i in range(1, 10)],
                    "pitchers": [605483],
                    "players": {
                        f"ID{700000 + i}": _player(700000 + i, f"Away Batter {i}", "CF")
                        | {
                            "stats": {
                                "batting": {
                                    "plateAppearances": 4,
                                    "atBats": 4,
                                    "hits": 1,
                                    "doubles": 1 if i == 5 else 0,
                                    "triples": 0,
                                    "homeRuns": 0,
                                    "baseOnBalls": 0,
                                    "strikeOuts": 1,
                                    "runs": 0,
                                    "rbi": 0,
                                }
                            }
                        }
                        for i in range(1, 10)
                    }
                    | {
                        "ID605483": {
                            "person": {"id": 605483, "fullName": "Brayan Bello"},
                            "position": {"abbreviation": "P"},
                            "stats": {
                                "pitching": {
                                    "inningsPitched": "5.0",
                                    "battersFaced": 22,
                                    "numberOfPitches": 89,
                                    "strikeOuts": 5,
                                    "baseOnBalls": 3,
                                    "hits": 6,
                                    "homeRuns": 1,
                                    "earnedRuns": 4,
                                }
                            },
                        }
                    },
                },
            },
            "officials": [
                {"official": {"id": 427111, "fullName": "Angel Hernandez"}, "officialType": "Home Plate"},
                {"official": {"id": 427222, "fullName": "First Base Ump"}, "officialType": "First Base"},
            ],
        },
        "linescore": {
            "currentInning": 9,
            "teams": {"home": {"runs": 5}, "away": {"runs": 3}},
            "innings": [
                {"num": 1, "home": {"runs": 1}, "away": {"runs": 0}},
                {"num": 2, "home": {"runs": 0}, "away": {"runs": 2}},
                {"num": 3, "home": {"runs": 2}, "away": {"runs": 0}},
                {"num": 4, "home": {"runs": 0}, "away": {"runs": 0}},
                {"num": 5, "home": {"runs": 1}, "away": {"runs": 1}},
                {"num": 6, "home": {"runs": 0}, "away": {"runs": 0}},
                {"num": 7, "home": {"runs": 1}, "away": {"runs": 0}},
                {"num": 8, "home": {"runs": 0}, "away": {"runs": 0}},
                # Home leads after the top of the 9th, so the home half is not
                # played and carries no "runs" key at all.
                {"num": 9, "away": {"runs": 0}},
            ],
        },
    },
}

STATCAST_CSV = "\n".join(
    [
        "game_pk,game_date,at_bat_number,pitch_number,inning,inning_topbot,batter,pitcher,"
        "stand,p_throws,events,description,type,zone,balls,strikes,outs_when_up,on_1b,on_2b,"
        "on_3b,pitch_type,release_speed,plate_x,plate_z,sz_top,sz_bot,launch_speed,launch_angle,"
        "estimated_woba_using_speedangle,woba_value,woba_denom,delta_run_exp,bat_score,fld_score,bb_type",
        "776001,2025-04-01,1,1,1,Top,700001,543037,L,R,,called_strike,S,5,0,0,0,,,,FF,97.1,0.05,2.41,3.4,1.6,,,,,,-0.041,0,0,",
        "776001,2025-04-01,1,2,1,Top,700001,543037,L,R,strikeout,swinging_strike,S,14,0,2,0,,,,SL,88.2,0.61,1.42,3.4,1.6,,,,0,1,-0.118,0,0,",
        "776001,2025-04-01,2,1,1,Top,700002,543037,R,R,home_run,hit_into_play,X,5,0,0,1,,,,FF,96.4,-0.11,2.30,3.5,1.7,108.3,27,1.842,2.0,1,1.402,0,0,fly_ball",
        "776001,2025-04-01,3,1,1,Top,700003,543037,L,R,,ball,B,13,0,0,1,,,,CH,87.9,-1.42,0.91,3.4,1.6,,,,,,0.032,0,1,",
    ]
) + "\n"

RETROSHEET_EVENTS = """id,NYA202504010
version,2
info,visteam,BOS
info,hometeam,NYA
info,site,NYC21
info,date,2025/04/01
start,bos001,"Away Batter 1",0,1,8
start,nya001,"Home Batter 1",1,1,7
play,1,0,bos001,00,CX,S8/L
play,1,0,bos002,12,BCX,D7/F.1-3
play,1,0,bos003,32,BBBCS,K
play,1,0,bos004,01,CX,64(1)3/GDP
play,1,1,nya001,00,X,HR/F
play,2,0,bos005,11,BCX,S9.2XH(9E2)
play,2,0,bos006,00,X,8/F
play,2,0,bos007,22,BBCCX,W
play,2,0,bos008,00,X,63/G
"""

ODDS_API = [
    {
        "id": "abc123",
        "sport_key": "baseball_mlb",
        "commence_time": "2025-04-01T17:05:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2025-04-01T16:50:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2025-04-01T16:50:00Z",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -155},
                            {"name": "Boston Red Sox", "price": 141},
                        ],
                    }
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2025-04-01T16:48:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2025-04-01T16:48:00Z",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -170},
                            {"name": "Boston Red Sox", "price": 144},
                        ],
                    }
                ],
            },
        ],
    },
    {
        # Second game of the doubleheader. Its commence time is the only thing
        # separating it from the first.
        "id": "abc124",
        "sport_key": "baseball_mlb",
        "commence_time": "2025-04-01T23:05:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2025-04-01T22:50:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2025-04-01T22:50:00Z",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -120},
                            {"name": "Boston Red Sox", "price": 110},
                        ],
                    }
                ],
            }
        ],
    },
]

KALSHI_MARKETS = {
    "markets": [
        {
            "ticker": "KXMLBGAME-25APR01NYYBOS-NYY",
            "event_ticker": "KXMLBGAME-25APR01NYYBOS",
            "title": "Will the New York Yankees win?",
            "close_time": "2025-04-01T21:00:00Z",
            "yes_bid": 59,
            "yes_ask": 62,
            "last_price": 60,
            "volume": 4210,
            "status": "active",
        },
        {
            "ticker": "KXMLBGAME-UNPARSEABLE",
            "event_ticker": "KXMLBGAME-XX",
            "title": "Something the parser has never seen",
            "close_time": "2025-04-01T21:00:00Z",
            "yes_bid": 40,
            "yes_ask": 44,
        },
    ],
    "cursor": "",
}

KALSHI_ORDERBOOK = {
    "orderbook": {
        # Thin bid, deep offer: the mid is a lie and the depth columns are what
        # let a later stage notice.
        "yes": [[59, 12], [58, 40], [57, 130]],
        "no": [[38, 500], [37, 900]],
    }
}

POLYMARKET_EVENTS = [
    {
        "id": "evt1",
        "title": "Yankees vs Red Sox",
        "endDate": "2025-04-01T21:00:00Z",
        "markets": [
            {
                "id": "m1",
                "conditionId": "0xabc",
                "question": "Will the New York Yankees win?",
                "endDate": "2025-04-01T21:00:00Z",
                "clobTokenIds": '["11111","22222"]',
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.60","0.40"]',
                "bestBid": 0.59,
                "bestAsk": 0.62,
                "volume": "18400",
            }
        ],
    }
]

POLYMARKET_BOOK = {
    "market": "0xabc",
    "asset_id": "11111",
    "bids": [{"price": "0.59", "size": "120"}, {"price": "0.58", "size": "400"}],
    "asks": [{"price": "0.62", "size": "90"}, {"price": "0.63", "size": "250"}],
}

FANGRAPHS_BATTERS = [
    {
        "playerid": "19755",
        "xMLBAMID": 600001,
        "PlayerName": "Home Batter 1",
        "Team": "NYY",
        "PA": 640,
        "AB": 560,
        "H": 152,
        "2B": 30,
        "3B": 2,
        "HR": 28,
        "BB": 68,
        "SO": 140,
        "HBP": 6,
        "wOBA": 0.352,
        "wRCplus": 128,
    },
    {
        # No MLBAM id: unjoinable, and the resolution-rate check has to see it.
        "playerid": "99999",
        "PlayerName": "Unmapped Player",
        "Team": "BOS",
        "PA": 400,
        "H": 90,
        "2B": 18,
        "3B": 1,
        "HR": 12,
        "BB": 30,
        "SO": 95,
        "wOBA": 0.310,
    },
]

OPEN_METEO = {
    "latitude": 40.83,
    "longitude": -73.93,
    "hourly": {
        "time": ["2025-04-01T16:00", "2025-04-01T17:00", "2025-04-01T18:00"],
        "temperature_2m": [54.1, 56.3, 57.0],
        "relative_humidity_2m": [61, 58, 55],
        "surface_pressure": [1014.2, 1013.9, 1013.5],
        "wind_speed_10m": [8.1, 11.4, 12.0],
        "wind_direction_10m": [210, 205, 200],
        "precipitation_probability": [5, 5, 10],
        "cloud_cover": [30, 25, 20],
    },
}


def build() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "mlb_schedule.json").write_text(json.dumps(SCHEDULE, indent=2))
    (HERE / "mlb_game_feed.json").write_text(json.dumps(GAME_FEED, indent=2))
    (HERE / "statcast.csv").write_text(STATCAST_CSV)
    (HERE / "odds_api.json").write_text(json.dumps(ODDS_API, indent=2))
    (HERE / "kalshi_markets.json").write_text(json.dumps(KALSHI_MARKETS, indent=2))
    (HERE / "kalshi_orderbook.json").write_text(json.dumps(KALSHI_ORDERBOOK, indent=2))
    (HERE / "polymarket_events.json").write_text(json.dumps(POLYMARKET_EVENTS, indent=2))
    (HERE / "polymarket_book.json").write_text(json.dumps(POLYMARKET_BOOK, indent=2))
    (HERE / "fangraphs_batters.json").write_text(json.dumps(FANGRAPHS_BATTERS, indent=2))
    (HERE / "open_meteo.json").write_text(json.dumps(OPEN_METEO, indent=2))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("2025NYA.EVA", RETROSHEET_EVENTS)
    (HERE / "retrosheet_2025.zip").write_bytes(buffer.getvalue())

    print(f"wrote fixtures to {HERE}")


if __name__ == "__main__":
    build()
