# Connect the Harness to the Team Ollama Host

This guide explains how to run the harness on a teammate's computer while using
Devstral Small 2 through Ollama on a Windows host. Tailscale carries the connection
over the team's private network; do not expose Ollama through a router or the
public internet.

## Understand where the harness runs

Each teammate runs the harness locally. Only model inference runs on the host PC.

- The harness's file tools read files available to the teammate's local Python
  process, not files on the Ollama host.
- Prompts and file excerpts selected by the harness are sent to the host for model
  inference.
- The current tools can only list, find, and read files, but their read access is
  not restricted to the repository. Run the harness under an account that has
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

The script checks Ollama and Tailscale, optionally switches to the configured
previously authenticated account, and runs `tailscale up` when disconnected. It
then starts the loopback Ollama API when needed, pulls the configured model,
creates the private TCP Serve rule, and prints the URL teammates should use. It
intentionally does not install desktop applications, perform a new Tailscale
sign-in, or edit the centralized tailnet Grant. The manual steps below explain
those prerequisites and security decisions.

The provisioning values are kept in the Git-ignored `.env`. Some are shared by
the team, while the account selection belongs to the individual computer:

| Setting | Scope | Used by | Purpose |
| --- | --- | --- | --- |
| `HARNESS_OLLAMA_HOST` | Same for all teammates | Teammate script and harness | Reachable base URL printed by the host script |
| `HARNESS_MODEL` | Same for host and teammates | Both scripts and harness | Exact model to pull on the host and request from teammates |
| `HARNESS_TAILSCALE_ACCOUNT` | Per computer/person | Both scripts | Optional remembered Tailscale login to select; each teammate normally uses their own email, while an empty value keeps the active account |
| `HARNESS_OLLAMA_LOCAL_HOST` | Host only | Host script | Loopback-only Ollama API forwarded by Tailscale |
| `HARNESS_TAILSCALE_SERVE_PORT` | Host only; reflected in the shared host URL | Host script | Private tailnet TCP port exposed by Tailscale Serve |

Command-line parameters override these values for one run; for example,
`.\setup-host.ps1 -TailscaleAccount user@example.com -ServePort 11435`.
Account names are identifiers only—the scripts never collect passwords or
authentication keys.

### 1. Install Ollama and the model

Install Ollama for Windows from <https://ollama.com/download>, open a new
PowerShell window, and confirm the service is running:

```powershell
ollama --version
curl.exe http://127.0.0.1:11434
```

Install the exact model used by the harness:

```powershell
ollama pull devstral-small-2:latest
ollama list
```

Test the local API. The JSON response should list
`devstral-small-2:latest`:

```powershell
curl.exe --max-time 15 http://127.0.0.1:11434/api/tags
```

Keep Ollama on its default loopback address. Do not set `OLLAMA_HOST` to
`0.0.0.0`, create a router port-forwarding rule, or expose port `11434` publicly.

### 2. Install and connect Tailscale

Install Tailscale from <https://tailscale.com/download/windows>, sign in, and
confirm the host is connected:

```powershell
tailscale status
tailscale ip -4
```

The second command prints the host's private `100.x.x.x` address. If MagicDNS is
enabled, teammates can instead use the stable device name shown by
`tailscale status`.

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

### 1. Install Git and clone the harness

Install Git, then clone the repository using the URL provided by the repository
owner:

```powershell
git clone REPOSITORY_URL
cd harness-prototype
Copy-Item .env.example .env
```

### 2. Configure and run the teammate setup

Open `.env` and set the team-shared Ollama address and model. Set
`HARNESS_TAILSCALE_ACCOUNT` to this teammate's previously authenticated login
when the computer has multiple remembered accounts; otherwise leave it empty:

```dotenv
HARNESS_OLLAMA_HOST=http://HOST_DEVICE_NAME:11434
HARNESS_MODEL=devstral-small-2:latest
HARNESS_TAILSCALE_ACCOUNT=teammate@example.com
```

If MagicDNS is unavailable, replace `HOST_DEVICE_NAME` with the host's private
`100.x.x.x` address. The host value must be only the HTTP base address; do not
append `/api/tags`, `/api/chat`, or another API path.

Run the setup:

```powershell
.\setup.ps1
```

The script uses Windows Package Manager to install missing copies of `uv` and
Tailscale, uses `uv` to install Python 3.12, connects Tailscale, checks the remote
model, preserves other `.env` values, and installs the locked dependencies. If
`winget` is unavailable or blocked by organizational policy, install `uv` and
Tailscale manually before running it. Approve any Windows installer prompt and
sign in with the identity the host owner authorized if Tailscale opens a browser.
If prompted to select a tailnet, choose the team's tailnet.

Add `-Launch` to start the harness immediately. The `-OllamaHost`, `-Model`, and
`-TailscaleAccount` parameters override `.env` for that run and save the selected
values back to it. For example:

```powershell
.\setup.ps1 -OllamaHost http://HOST_DEVICE_NAME:11434 -Launch
```

If Windows blocks the script because of its execution policy, inspect the active
policies with `Get-ExecutionPolicy -List` and follow the organization's approved
policy. On an unmanaged computer, this process-only setting allows locally
created scripts without changing the persistent user or machine policy:

```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
.\setup.ps1
```

The script checks that the host is visible and that its Ollama API contains the
configured model. Equivalent manual checks are:

```powershell
tailscale status
tailscale ping HOST_DEVICE_NAME
```

For installations connected to multiple accounts, use `tailscale switch --list`
and `tailscale switch user@example.com` to select the correct one.

### 3. Verify the Ollama connection manually when needed

Use the host's MagicDNS device name when available:

```powershell
curl.exe --max-time 15 http://HOST_DEVICE_NAME:11434/api/tags
```

If the name does not resolve, use the host's private Tailscale IPv4 address:

```powershell
curl.exe --max-time 15 http://HOST_TAILSCALE_IP:11434/api/tags
```

The response should contain `devstral-small-2:latest`. Do not use `127.0.0.1`
on a teammate's computer; that address refers to the teammate's computer.

### 4. Launch the harness

Unless `-Launch` was used, start the harness from the checkout:

```powershell
uv run python -m harness_prototype
```

The setup script has already installed the locked Python dependencies. Teammates
do not need to install Ollama or download the model locally. The project's `.env`
file is ignored by Git and should not be committed.

## Troubleshooting

### The host does not appear

- Confirm the teammate signed in with the authorized identity and selected the
  correct tailnet.
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
curl.exe --verbose --max-time 15 http://HOST_DEVICE_NAME:11434/api/tags
```

If the local Ollama request works but the remote request fails, review both the
Serve rule and the Tailscale Grant. If the API works but the harness does not,
check `HARNESS_OLLAMA_HOST` for an API path, credentials, whitespace, or the wrong
port, and restart the harness after editing `.env`.

### The harness reports that the model is missing

Run `ollama list` on the host and copy the model identifier exactly, including its
tag, into the teammate's `HARNESS_MODEL` setting.

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
