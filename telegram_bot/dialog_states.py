from typing import List, Optional

FIRST, SECOND = range(2)

CHOOSE_STATS, TEAM_STATS, PLAYER_STATS, DAY_DIGEST, PLAYER_FIELD, PLAYER_GOALIE, TEAM_PROCENT_WINS, \
    TEAM_POWER_PLAY, TEAM_POWER_KILL, PLAYER_POINTS, PLAYER_GOALS, PLAYER_ASSISTS, \
    PLAYER_PLUS_MINUS, PLAYER_PENALTIES, PLAYER_HITS, PLAYER_BLOCKS, PLAYER_ICE_TIME, GOALIE_WINS, GOALIE_PERCENTAGE, \
    GOALIE_SHOOTOUTS, END_CONVERSATION = range(21)


def build_menu(
    buttons: list,
    n_cols: int,
    header_buttons: Optional[list] = None,
    footer_buttons: Optional[list] = None,
) -> List[list]:
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)
    return menu
