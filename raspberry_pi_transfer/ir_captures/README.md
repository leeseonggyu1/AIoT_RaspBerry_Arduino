# IR Capture Index

Captured on 2026-06-09 with an Arduino Uno and a 38 kHz IR receiver on D4.

Use these files as raw IR command sources for the AC controller.

## Temperature Change Commands

- `set_cool_18.txt` through `set_cool_28.txt`
- Use when the AC/remote is already on and the target temperature should change.
- Arduino serial commands: `AC_SET_COOL_18` through `AC_SET_COOL_28`

## Power On Commands

- `on_cool_18.txt` through `on_cool_28.txt`
- Use when the AC should be turned on directly to the target temperature.
- Arduino serial commands: `AC_ON_COOL_18` through `AC_ON_COOL_28`

## Power Off Command

- `ac_off.txt`
- Use when the AC should be turned off.
- Arduino serial command: `AC_OFF`

## Dry Mode Commands

- `on_dry.txt`
- Use when humidity is high but the room is not hot enough for cooling.
- Arduino serial command: `AC_ON_DRY`
- `dry_off.txt`
- Use when turning off after dry mode, or compare with `ac_off.txt` before choosing a single off command.
- Arduino serial command: `AC_DRY_OFF`

## Notes

- All final commands were captured with `count=227` and `overflow=NO`.
- Expected remote settings: cooling mode, fan auto, swing default, extra modes off.
- AC remotes usually transmit the full state, so mode/fan/swing settings should remain consistent.
