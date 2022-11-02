# NHL_bot


### Description


### Pipeline
Every day we take and update all team statistic.
The function of it is in pipeline
It has functions which get Data from API and warehouse it for three paths:
Players, teams, game stats.
Game stats is the main path of this.
It gets any stats of the game, 
    goals stat (who is scored, assisted, time of it and other), 
    team stats (shots, blocks, power play, short-handed and other)
    player and goalie stats in game
It makes in 3 files, pipeline, which collects all files and get result
Columns, it is file which shows about structure of DataFrame
Functions, it has some functions of pipeline
And pipeline which collect all of it together