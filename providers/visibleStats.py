"""Conservative fallback for numeric fields rendered in a match's player sheet."""
import re

FIELDS={
 'Minutes played':'minutesPlayed','Goals':'goals','Assists':'goalAssist',
 'Total shots':'totalShots','Shots on target':'onTargetScoringAttempt',
 'Shots off target':'shotOffTarget','Blocked shots':'blockedScoringAttempt',
 'Touches':'touches','Key passes':'keyPass','Big chances created':'bigChanceCreated',
 'Big chances missed':'bigChanceMissed','Tackles':'totalTackle','Interceptions':'interceptionWon',
 'Clearances':'totalClearance','Blocked shots (defending)':'outfielderBlock',
 'Possession lost':'possessionLostCtrl','Fouls':'fouls','Was fouled':'wasFouled',
 'Offsides':'totalOffside','Total saves':'saves','Goals prevented':'goalsPrevented',
 'Saves from inside box':'savedShotsFromInsideTheBox','Punches':'punches','High claims':'goodHighClaim',
 'Expected goals (xG)':'expectedGoals','Expected assists (xA)':'expectedAssists',
 'Dispossessed':'dispossessed','Hit woodwork':'hitWoodwork',
}
PAIRS={
 'Accurate passes':('totalPass','accuratePass'),
 'Long balls (acc.)':('totalLongBalls','accurateLongBalls'),
 'Crosses (acc.)':('totalCross','accurateCross'),
 'Dribble attempts (succ.)':('totalContest','wonContest'),
 'Runs out (succ.)':('totalKeeperSweeper','accurateKeeperSweeper'),
}


def parse_visible_stats(text):
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    result={};observed={}
    for label,value in zip(lines,lines[1:]):
        if label in FIELDS and re.fullmatch(r'-?\d+(?:\.\d+)?[\u2032\']?',value):
            number=float(value.rstrip("'\u2032"));result[FIELDS[label]]=int(number) if number.is_integer() else number
            observed[label]=value
        elif label in PAIRS:
            # e.g. total (successful). Percentages are never inverted to counts.
            match=re.fullmatch(r'(\d+)\s*\((\d+)\)',value)
            if match:
                total,completed=map(int,match.groups())
                if completed<=total:
                    result.update(zip(PAIRS[label],(total,completed)));observed[label]=value
    return result,observed
