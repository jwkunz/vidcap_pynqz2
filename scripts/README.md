# scripts

Generators that write **standalone Tcl** for the vidcap workspaces and IP. The Tcl itself goes under
`workspace/` (untracked, regenerated on demand); only the generators live in git, so the Tcl can never
drift from the sources it was made from.

| Generator | Writes | Source of truth |
|---|---|---|
| `gen_hls_tcl.py [core ...]` | `workspace/vitishls/generated/<core>_sim.tcl`, `<core>_build.tcl`, `run_all.sh` | `src/cores/<core>/hls.cfg.in` (and `sim_dims`) |
| `gen_vivado_tcl.sh` | `workspace/vivado/generated/vidcap_project_generated.tcl`, `vidcap_bd_generated.tcl`, `add_hls_ip.tcl` | the Vivado project built from `src/overlay/` |

Run both with `make gen-tcl`.

## HLS cores

`<core>_sim.tcl` rebuilds the HLS workspace at the small simulation frame size and runs C-simulation,
synthesis and C/RTL co-simulation. `<core>_build.tcl` does the production build (1280x720, free-running
`ap_ctrl_none`) and exports the IP to `workspace/vitishls/generated/ip/<core>.zip`.

```
python3 scripts/gen_hls_tcl.py                  # every core
source ~/AMD/2025.2/Vitis/settings64.sh
vitis-run --mode hls --tcl workspace/vitishls/generated/vc_filter_build.tcl
workspace/vitishls/generated/run_all.sh         # or all of them
```

## Vivado project and IP

`gen_vivado_tcl.sh` opens the built project (creating its block design first if there is none) and writes:

* `vidcap_project_generated.tcl`: recreates the whole project, block design embedded, from any directory.
  Re-runnable (it overwrites its own previous output at `workspace/vivado/vidcap_regen`).
* `vidcap_bd_generated.tcl`: the block design alone, with layout, to import into another project.
* `add_hls_ip.tcl`: adds the packaged vidcap HLS IPs (`vc_filter`, `vc_overlay`, `vc_snapshot`) to a
  project's IP catalogue.

```
vivado -mode batch -source workspace/vivado/generated/vidcap_project_generated.tcl
```

The recreated project needs the same prerequisites as the normal build: the PYNQ reference files
(`src/overlay/scripts/fetch_refs.sh`) and the unpacked HLS IP (`make ip`).
