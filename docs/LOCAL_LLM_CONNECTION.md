# Connect FLUX to the Team Ollama Host

This guide explains how to run FLUX on a teammate's computer while using
Devstral Small 2 through Ollama on a Windows host. Tailscale carries the connection
over the team's private network; do not expose Ollama through a router or the
public internet.

## Understand where FLUX runs

Each teammate runs FLUX locally. Only model inference runs on the host PC.

- FLUX's file tools read files available to the teammate's local Python
  process, not files on the Ollama host.
- Prompts and file excerpts selected by FLUX are sent to the host for model
  inference.
- The current tools can only list, find, and read files, but their read access is
  not restricted to the repository. Run FLUX under an account that has
  access only to data the teammate is comfortable sending to the model host.
- Ollama does not add per-user authorization to this connection. Tailscale identity
  and access rules are the security boundary, so only trusted teammates should be
  able to reach TCP port `11434`.

## Host setup

For automated setup, clone this repository on the host, copy `.env.example` to
`.env`, adjust the host provisioning values, and run:

```powershell
Copy-Item .env.example .env
.\setup-host.ps1
```

The script installs missing copies of Ollama and Tailscale through Windows Package
Manager, optionally switches to the configured previously authenticated profile,
and runs `tailscale up` when disconnected. It then starts the loopback Ollama API
when needed, pulls the configured models, creates the private TCP Serve rule, and
prints the URL teammates should use. It does not edit the centralized tailnet
Grant. Tailscale may still require browser authentication after installation. The
manual steps below explain the same setup and the required security decisions.

The provisioning values are kept in the Git-ignored `.env`. Some are shared by
the team, while the account selection belongs to the individual computer:

| Setting | Scope | Used by | Purpose |
| --- | --- | --- | --- |
| `HARNESS_OLLAMA_HOST` | Same for all teammates | Teammate script and FLUX | Reachable base URL printed by the host script |
| `HARNESS_MODELS` | Same for host and teammates | Both scripts and FLUX | Comma-separated models to install, verify, and offer at startup |
| `HARNESS_TAILSCALE_PROFILE` | Per computer | Both scripts | Optional entry from `tailscale switch --list` that can reach the Ollama host. An empty value keeps the active profile. |
| `HARNESS_OLLAMA_LOCAL_HOST` | Host only | Host script | Loopback-only Ollama API forwarded by Tailscale |
| `HARNESS_TAILSCALE_SERVE_PORT` | Host only; reflected in the shared host URL | Host script | Private tailnet TCP port exposed by Tailscale Serve |

Command-line parameters override these values for one run; for example,
`.\setup-host.ps1 -TailscaleProfile user@example.com -ServePort 11435`.
Account names are identifiers only—the scripts never collect passwords or
authentication keys.

### 1. Install Ollama and the models manually

`setup-host.ps1` installs Ollama through Winget when it is missing. If Winget is
unavailable or blocked by organizational policy, install Ollama for Windows from
<https://ollama.com/download>, open a new PowerShell window, and confirm the
service is running:

```powershell
ollama --version
curl.exe http://127.0.0.1:11434
```

Install the default models used by FLUX:

```powershell
ollama pull devstral-small-2:latest
ollama pull qwen3:8b
ollama pull qwen3-vl:8b
ollama list
```

`qwen3-vl:8b` provides image understanding, visual and spatial reasoning,
image-informed coding, and tool use. It requires Ollama 0.12.7 or newer.

Test the local API. The JSON response should list all configured models:

```powershell
curl.exe --max-time 15 http://127.0.0.1:11434/api/tags
```

Keep Ollama on its default loopback address. Do not set `OLLAMA_HOST` to
`0.0.0.0`, create a router port-forwarding rule, or expose port `11434` publicly.

### 2. Install and connect Tailscale manually

`setup-host.ps1` also installs Tailscale through Winget when it is missing. If a
manual installation is required, install Tailscale from
<https://tailscale.com/download/windows>, sign in, and confirm the host is
connected:

```powershell
tailscale status
tailscale ip -4
```

The second command prints the host's private `100.x.x.x` address. Use this IPv4
address for `HARNESS_OLLAMA_HOST`; Ollama may reject a MagicDNS name in the HTTP
`Host` header even when that name resolves and Tailscale connectivity works.

Invite only the teammates who need access. Depending on how the team manages its
tailnet, either invite them as tailnet users or share only this host with them.

### 3. Restrict access to the Ollama port

Before inviting teammates, review the tailnet policy in the Tailscale admin
console. A tailnet that still has Tailscale's original default allow-all policy
may let members reach more devices and ports than intended.

Create a least-privilege Grant that permits only the selected teammate identities
to reach TCP port `11434` on this host. Use the visual policy editor or adapt the
following illustrative policy to the existing tailnet policy:

```jsonc
{
  "groups": {
    "group:harness-users": [
      "teammate1@example.com",
      "teammate2@example.com"
    ]
  },
  "hosts": {
    "ollama-host": "100.x.x.x"
  },
  "grants": [
    {
      "src": ["group:harness-users"],
      "dst": ["ollama-host"],
      "ip": ["tcp:11434"]
    }
  ]
}
```

Do not replace unrelated existing policy sections with this example. Substitute
the actual teammate identities and host address, preserve required administrator
access, and use the admin console's policy validation before saving. Tailscale's
current Grants documentation is at
<https://tailscale.com/docs/features/access-control/grants>.

### 4. Forward the private Tailscale port

Keep Ollama listening on loopback and have Tailscale forward private traffic:

```powershell
tailscale serve --bg --tcp=11434 tcp://127.0.0.1:11434
tailscale serve status
```

The status should show Tailscale port `11434` forwarding to
`127.0.0.1:11434`. Tailscale documents this raw TCP-forwarder form at
<https://tailscale.com/docs/reference/tailscale-cli/serve>.

### 5. Keep the host available

Remote use requires the host to remain powered on and awake, connected to the
internet and Tailscale, and running Ollama. Concurrent requests share its CPU,
GPU, RAM, and VRAM. Coordinating one active evaluation at a time may give more
predictable performance on consumer hardware.

## Teammate setup

### 1. Install Git and clone FLUX

Install Git, then clone the repository using the URL provided by the repository
owner:

```powershell
git clone REPOSITORY_URL
cd harness-prototype
Copy-Item .env.example .env
```

### 2. Configure and run the teammate setup

Open `.env` and set the team-shared Ollama address and model. Set
`HARNESS_TAILSCALE_PROFILE` to the entry from `tailscale switch --list` whose
tailnet contains or has access to the Ollama host; otherwise leave it empty:

```dotenv
HARNESS_OLLAMA_HOST=http://HOST_TAILSCALE_IP:11434
HARNESS_MODELS=devstral-small-2:latest,qwen3:8b,qwen3-vl:8b
HARNESS_TAILSCALE_PROFILE=profile@example.com
```

Use the host's private `100.x.x.x` Tailscale address. The host value must be only
the HTTP base address; do not append `/api/tags`, `/api/chat`, or another API path.

Run the setup:

```powershell
.\setup.ps1
```

The script uses Windows Package Manager to install missing copies of `uv` and
Tailscale, uses `uv` to install Python 3.12, connects Tailscale, checks the remote
models, preserves other `.env` values, and installs the locked dependencies. If
`winget` is unavailable or blocked by organizational policy, install `uv` and
Tailscale manually before running it. Approve any Windows installer prompt and
sign in with the identity the host owner authorized if Tailscale opens a browser.
If prompted to select a tailnet, choose the team's tailnet.

Add `-Launch` to start FLUX immediately. The `-OllamaHost`, `-Models`,
and `-TailscaleProfile` parameters override `.env` for that run and
save the selected values back to it. `-Models` accepts a comma-separated string.
For example:

```powershell
.\setup.ps1 -OllamaHost http://HOST_TAILSCALE_IP:11434 -Launch
```

If Windows blocks the script because of its execution policy, inspect the active
policies with `Get-ExecutionPolicy -List` and follow the organization's approved
policy. On an unmanaged computer, this process-only setting allows locally
created scripts without changing the persistent user or machine policy:

```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
.\setup.ps1
```

The script checks that the host is visible and that its Ollama API contains all
configured models. Equivalent manual checks are:

```powershell
tailscale status
tailscale ping HOST_TAILSCALE_IP
```

For installations connected to multiple accounts, use `tailscale switch --list`
and `tailscale switch user@example.com` to select the teammate's own account.
Do not sign a teammate's computer into the host owner's account. Instead, invite
the teammate to the host's tailnet or share the Ollama host with their Tailscale
identity, wait for them to accept, and grant that identity access to TCP port
`11434`.

### 3. Verify the Ollama connection manually when needed

Use the host's private Tailscale IPv4 address:

```powershell
curl.exe --max-time 15 http://HOST_TAILSCALE_IP:11434/api/tags
```

The response should contain every model listed in `HARNESS_MODELS`. Do not use
`127.0.0.1` on a teammate's computer; that address refers to the teammate's computer.

### 4. Launch FLUX

Unless `-Launch` was used, start FLUX from the checkout:

```powershell
uv run python -m harness_prototype
```

The setup script has already installed the locked Python dependencies. Teammates
do not need to install Ollama or download the models locally. The project's `.env`
file is ignored by Git and should not be committed.

## Troubleshooting

### The host does not appear

- Confirm the teammate is using their own identity, has accepted the tailnet or
  machine-share invitation, and selected the tailnet/profile containing the host.
- Check `tailscale switch --list` on the teammate's computer.
- Confirm in the Tailscale admin console that the invitation was accepted and the
  correct device is connected.

### Tailscale ping works but the API times out

On the host, run:

```powershell
curl.exe --max-time 15 http://127.0.0.1:11434/api/tags
tailscale status
tailscale serve status
```

On the teammate's computer, run:

```powershell
curl.exe --verbose --max-time 15 http://HOST_TAILSCALE_IP:11434/api/tags
```

If the local Ollama request works but the remote request fails, review both the
Serve rule and the Tailscale Grant. If the API works but FLUX does not,
check `HARNESS_OLLAMA_HOST` for an API path, credentials, whitespace, or the wrong
port, and restart FLUX after editing `.env`.

### FLUX reports that the model is missing

Run `ollama list` on the host and copy each model identifier exactly, including its
tag, into the teammate's `HARNESS_MODELS` setting. The selector starts on the
first model in that list.

## Restarting and stopping access

To stop Ollama and disconnect the host from Tailscale, run:

```powershell
.\stop-host.ps1
```

The script leaves downloaded models, `.env`, Tailscale Serve rules, and the
Tailscale Windows service in place. Running `.\setup-host.ps1` later reconnects
Tailscale, restarts Ollama, and restores the host without reconfiguration.

After restarting the host, verify:

```powershell
tailscale status
tailscale serve status
curl.exe --max-time 15 http://127.0.0.1:11434/api/tags
```

If the Serve rule is missing, recreate it:

```powershell
tailscale serve --bg --tcp=11434 tcp://127.0.0.1:11434
```

The model does not need to be started manually after every reboot. Ollama loads
an installed model when the first request arrives, so that request may be slower.

To stop sharing the API without disconnecting Tailscale:

```powershell
tailscale serve --tcp=11434 off
```

To unload the model from memory while leaving Ollama available:

```powershell
ollama stop devstral-small-2:latest
```

## Offboarding a teammate

When a teammate should no longer have access:

1. Remove their identity from `group:harness-users` or the equivalent Grant.
2. Remove or suspend their Tailscale membership, or revoke the device share if
   only this host was shared.
3. Remove their repository access when appropriate.
4. From their former access path, verify that port `11434` is no longer reachable.
5. If nobody needs remote inference, disable the Serve rule with
   `tailscale serve --tcp=11434 off`.
