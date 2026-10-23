# Testing safely: Windows Sandbox and VirtualBox

Test any new or risky action, such as one that closes windows, touches files or changes system settings, in a throwaway Windows first. If something goes wrong, your real PC is not affected.

## Option 1: Windows Sandbox (Windows 10/11 Pro, Enterprise or Education)

Windows Sandbox is a clean, temporary copy of Windows. Everything inside it is deleted when you close it.

### Turn it on (once)

1. Your PC needs virtualisation enabled in BIOS/UEFI. In Task Manager, open **Performance**, then **CPU**, and check that **Virtualization** says *Enabled*.
2. Open **Turn Windows features on or off**, tick **Windows Sandbox**, and click OK.
3. Restart the PC.

Or, in an administrator PowerShell:

```powershell
Enable-WindowsOptionalFeature -FeatureName "Containers-DisposableClientVM" -All -Online
```

Windows Sandbox is **not available on Windows Home**. Use Option 2 instead.

### Use it

1. Create `.env` in the project folder on your real PC (copy `.env.example` and add your Sarvam key).
2. Open `sandbox\Startup-Sarvam.wsb` in Notepad. Change `<HostFolder>` to the full path of your project folder, for example `C:\Users\YourName\Desktop\Startup-Sarvam`, and save.
3. Double-click `Startup-Sarvam.wsb`.
4. The sandbox starts and runs `sandbox_setup.ps1`. That script downloads Python, installs the dependencies and starts the assistant. The first start takes a few minutes.
5. Test your commands. Allow microphone access if Windows asks.
6. Close the Sandbox window. Everything inside it is wiped.

What the config does:

| Setting | Value | Why |
|---|---|---|
| Mapped folder | project folder, **read-only** | The sandbox can read the code but cannot change your real files |
| Networking | on | Needed for the Sarvam API |
| Audio input | on | Needed for the microphone |
| Clipboard | on | Lets you test `type_text` |
| Memory | 4 GB | Enough for Python and the window |

The database, logs and notes go to `C:\sarvam-data` inside the sandbox.

## Option 2: VirtualBox VM (free, works on Windows Home)

A virtual machine keeps its state between runs. **Snapshots** let you roll back to a clean state in seconds.

### Set up (once)

1. Install VirtualBox from https://www.virtualbox.org/ and its Extension Pack.
2. Download a Windows 11 ISO from https://www.microsoft.com/software-download/windows11. Microsoft also offers free evaluation VMs for developers.
3. In VirtualBox, click **New**. Pick the ISO and give the VM at least 4 GB RAM, 2 CPUs and a 64 GB disk. Install Windows.
4. Install **Guest Additions** (Devices, then Insert Guest Additions CD image) for shared folders and clipboard.
5. In the VM settings, open **Audio**. Tick **Enable Audio Input** and **Enable Audio Output**.
6. In the VM settings, open **Shared Folders**. Add your project folder, tick **Read-only** and **Auto-mount**.
7. Inside the VM, install Python 3.12 from python.org. Then run:

   ```powershell
   cd \\VBOXSVR\Startup-Sarvam
   python -m venv C:\sarvam-venv
   C:\sarvam-venv\Scripts\pip install -r requirements.txt
   ```

8. Shut down the VM. Then select it and open **Snapshots**. Click **Take** and name the snapshot `clean`.

### Test and roll back

1. Start the VM and run the assistant. Set writable folders first, because the share is read-only:

   ```powershell
   $env:DB_PATH="C:\sarvam-data\assistant.db"; $env:LOG_DIR="C:\sarvam-data\logs"; $env:NOTES_DIR="C:\sarvam-data\notes"
   cd \\VBOXSVR\Startup-Sarvam
   C:\sarvam-venv\Scripts\python main.py
   ```

2. Try the new action.
3. If anything breaks: power off the VM, select the `clean` snapshot and click **Restore**. The VM is back to its clean state.

## What to test in a sandbox first

- Any new action that closes programs, deletes, moves or overwrites files, or changes settings.
- Changes to the allow-list (`config/allowlist.yaml`: actions, risk levels, apps, safe shortcuts).
- Changes to the safety rules in `safety/` (guard, confirmation, emergency stop).
