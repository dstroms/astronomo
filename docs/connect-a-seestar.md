# Connect a real Seestar

Moved out of the README on 2026-11-21 with the text unchanged, apart from the links. The README keeps a short summary ([Connect a real Seestar](../README.md#connect-a-real-seestar)).

The README's quick start needs no telescope. This is how a Seestar S50 Pro (firmware 9.75) was connected. There are two ways to put the computer and the telescope on one network. **Use station mode (A) at home.** It keeps the internet, which the weather check and the Gaia comparison stars need during a mission. It has been tested at the bench only, not yet on the sky.

1. In the Seestar app, update the firmware. This unit was updated to **9.75**. Leave any later update until the check below has run.
2. **A. Station mode (the telescope joins your home Wi-Fi; tested 2026-11-20).**
   1. **Check your router's address range first.** Station mode fails with "network connect failed", after the Wi-Fi seems to join, when the router uses **`10.0.0.x`**. ZWO blocks that range because the telescope's own Wi-Fi uses it, and Xfinity gateways use it by default. To see yours, look at the address of the gateway your computer uses (macOS: `route -n get default | grep gateway`). If it starts `10.0.0.`, change the router to another range such as `192.168.1.x`. On an Xfinity gateway, open `http://10.0.0.1` (default login `admin` / `password` unless you changed it), then **Gateway**, **Connection**, **Local IP Network**, and change the first three parts of the Gateway Address, DHCP Beginning Address and DHCP Ending Address from `10.0.0` to `192.168.1`. The Xfinity app has the same setting under Connect, View WiFi equipment, Advanced Settings, LAN & WAN. **This changes the address of every device at home**, so reboot them afterwards, including any second router. Some rented gateways may not allow the change.
   2. In the Seestar app, open the Wi-Fi settings, turn on **Station Mode**, pick your home network and enter its password. Keep the telescope near the router. If it still fails, reset the telescope's Wi-Fi (the button on the base) and try again, switch cellular data off for the Seestar app, and try the 2.4 GHz band (some routers merge both bands under one name, which can make the telescope flip bands and lose station mode).
   3. Unplug any USB cable from the telescope. While it is mounted as a drive it does not answer on the network.
   4. In the app, open **Device management**, then **Assistive Features**, and switch on the **Alpaca** server. It stays off until you do, and a Wi-Fi change can switch it off again. Then close the Seestar app completely, so it is not holding the connection, and wait a minute or two.
   5. Find the telescope's address (the app's Wi-Fi details, or your router's device list). Check that it answers, with your own address in place of the example:

      ```bash
      curl "http://192.168.1.200:32323/management/v1/configureddevices"
      ```

      It lists the telescope, two cameras, two focusers, a filter wheel and a switch. The telescope's address can change, because it comes from your router. If your router lets you reserve it for the telescope, do (some, including a rented Xfinity gateway, may not). If it does not, leave `ASTRONOMO_SEESTAR_HOST` at its default, `seestar.local`: the name resolved to the telescope on this network, and `--hardware-check` passed with it (2026-11-20). If a name lookup is dropped on your network (guest networks and some switches do this), use the address instead and look it up again when the connection fails. Alpaca has no password, so anyone on the network can send it commands.
   6. Set `ASTRONOMO_SEESTAR_HOST` to that address, or leave it as `seestar.local` (step 4 below).
3. **B. The telescope's own Wi-Fi (the way it was first connected, 2026-11-13).** Connect the computer to the telescope's Wi-Fi (`S50_xxxxx`, address `10.0.0.1`). That network has no internet, and the computer loses every other network while it is joined. Fetch the forecast first (`python main.py --fetch-forecast`). If you need both here, tether a phone over USB for internet and put the tether above Wi-Fi in the macOS service order. That has not been tried with this telescope, and it breaks if the other network is also `10.0.0.x`.
4. In `.env`, set `ASTRONOMO_SEESTAR_TRANSPORT=alpaca`, and for station mode `ASTRONOMO_SEESTAR_HOST` to the telescope's address. Firmware 7.18 and newer needs the Alpaca transport: the default JSON-RPC transport wants a handshake Astronomo does not do. Set your site as well. A real run refuses the Denver placeholder.
5. Run the smoke test. It connects, reads the scope and parks. It does not slew or take a picture, and it needs no plan, no forecast and no internet:

```bash
ASTRONOMO_SEESTAR_TRANSPORT=alpaca ASTRONOMO_SEESTAR_HOST=192.168.1.200 python main.py --hardware-check
```

`--hardware-check` turns the telescope mock off for that one command. `--no-park` skips the park (use it when you do not want the arm to move). `--simulate` runs the same steps against the mock. Passed over station mode on 2026-11-20 with `--no-park`, with the computer also able to reach Open-Meteo and the Harvard VizieR mirror. A full mission over station mode has not been run yet.

What this telescope has not yet answered is the [open hardware questions](first-light/05-first-light-checklist.md). Why the JSON-RPC handshake is not implemented is in [the firmware note](first-light/03-firmware-and-authentication.md); host, port and other settings are in [Configuration](configuration.md). Read [Real hardware](real-hardware.md) and [the first-mission note](first-light/06-first-mission.md) before a mission.
