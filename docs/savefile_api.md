# Save-file API

`modules/savefile_api.py` reads local `.ir_solution` files without running
Foldit. The module is self-contained and can be copied into another project.

```python
from modules.savefile_api import get_basic_info, get_save_summary

info = get_basic_info(save_path)
print(info.save_name)      # str or None
print(info.player_name)    # str or None
print(info.foldit_score)   # float or None
print(info.errors)         # unavailable core field -> explanation

summary = get_save_summary(save_path)
print(summary.puzzle_id, summary.total_score)
print(summary.base_score, summary.bonus_score)
print(summary.solution_mode, summary.mode_player_id)
```

Both queries read the file once and extract name, player/puzzle identity,
score, and optional mode independently. A missing or unsupported block does
not discard the other fields. Missing values are `None`; a known zero score
remains `0.0`. If no fields can be read, the result contains `None` values and
errors explaining the missing core fields.

`FolditBasicInfo.errors` uses `save_name`, `player_name`, and `foldit_score`
keys. `FolditSaveSummary.errors` uses `save_name`, `player_name`, `puzzle_id`,
and `total_score`. Optional mode and score-breakdown fields can be `None`
without an error: their absence does not make the core metadata incomplete.

Downloaded shared solutions may contain a recorded total energy and a
zero-length per-residue energy array. The parser validates this layout using
its position after META and the following player record. It returns the
recorded total score; base and bonus remain `None` because their breakdown
cannot be established. Disulfide bonuses are not added to this total again.

To require all core fields, pass `strict=True` to either query. Missing core
fields then raise `FolditApiError`. File access errors always raise
`FolditApiError`, including in partial mode.

`get_save_name()`, `get_player_name()`, `get_foldit_score()`, and
`get_solution_mode_info()` require only their requested data and raise
`FolditApiError` if it cannot be read. `get_bonus_score()` returns `None`
when its breakdown is unknown. It can still return a known disulfide bonus
when the energy block is unavailable and RESI markers can be read.

`export_pdb()` uses residue coordinates independently of the metadata and
score queries. It raises `FolditApiError` if a PDB cannot be extracted.
