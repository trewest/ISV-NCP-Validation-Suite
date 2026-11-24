# NICo NVL72 provider

This provider configuration contains the non-destructive first-wave checks for
the GB300 NVL72 target host. It does not launch, reboot, power-cycle, reinstall,
or tear down a machine.

Set the target identifiers and local SSH key paths in the launchpad shell:

```bash
export NICO_MACHINE_ID='...'
export NICO_DPU_MACHINE_ID='...'
export NVL72_SSH_KEY_FILE="$HOME/.ssh/nvl72-host"
export NVL72_DPU_SSH_KEY_FILE="$HOME/.ssh/nvl72-dpu"
export NVL72_HOST='host-admin-address'
export NVL72_DPU_HOST='dpu-oob-address'
```

Validate the configuration before running it:

```bash
uv run isvctl test validate \
  -f isvctl/configs/providers/nico/config/bare_metal_nvl72.yaml
```

The scripts use `nico-admin-cli` for NICo queries and SSH only for read-only
host/DPU probes. Credentials and private keys must remain outside the
repository.
