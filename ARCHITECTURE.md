# Webex OBS Companion Architecture

```
+-------------------------------------------------------------+
|                     macOS User Session                      |
|                                                             |
|  +---------------------+      +--------------------------+  |
|  | Webex Desktop App   |      |        OBS Studio        |  |
|  | (CiscoCollabHost)   |      |  - WebSocket Server 4455 |  |
|  +----------+----------+      |  - Scene: Webex-Audio    |  |
|             |                 |  - Scene: Webex-Video    |  |
|             v                 +-------------+------------+  |
|     [ Process Polling ]                     ^               |
|             |                               | WebSocket v5  |
|             v                               v               |
|  +-------------------------------------------------------+  |
|  |            webex_obs Background Daemon                |  |
|  |                                                       |  |
|  |  - ProcessMonitor: Tracks meeting lifecycles          |  |
|  |  - OBSController: Starts/stops audio & video captures |  |
|  |  - WindowBinder: Quartz dynamic Webex window binding  |  |
|  |  - HotkeyListener: Cmd+Shift+V for instant video swap |  |
|  |  - Transcriber: Local Apple MLX Whisper pipeline     |  |
|  |  - WebexClient: Posts transcripts to My Agent space   |  |
|  |  - MediaCleaner: 14-day rolling media retention       |  |
|  +--------------------------+----------------------------+  |
|                             |                               |
|                             v                               |
|              +------------------------------+               |
|              | Apple MLX Whisper (Local GPU)|               |
|              +--------------+---------------+               |
+-----------------------------|-------------------------------+
                              v HTTPS API
               +------------------------------+
               |     Cisco Webex Platform     |
               |      (My Agent 1:1 Bot)      |
               +------------------------------+
```
