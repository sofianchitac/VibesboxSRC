import QtQuick
import QtWebSockets

// WebSocket client for the source_router daemon (:8080).
// Mirrors ui/app.js: receives full-state JSON messages, sends command JSON.
Item {
    id: root
    property string host: "localhost"

    property bool firstStateReceived: false
    property bool shuttingDown: false      // daemon acked shutdown/reboot
    property var sourcesMuted: ({})
    property var sourcesPlaying: ({})
    property int channels: 2
    property real inputRate: 0             // Hz, 0 = unknown
    property string btState: "idle"
    property string btDevice: ""
    property bool partyMode: false

    signal newState()

    function send(obj) {
        if (sock.status === WebSocket.Open)
            sock.sendTextMessage(JSON.stringify(obj))
    }
    function toggleMute(name) { send({ command: "toggle_mute", source: name }) }
    function setPartyMode(enabled) { send({ command: "set_party_mode", enabled: enabled }) }
    function unmuteAll() {
        for (var src in sourcesMuted)
            if (sourcesMuted[src])
                send({ command: "set_mute", source: src, muted: false })
    }
    function setBrightness(level) { send({ command: "set_brightness", level: level }) }
    function shutdown() { send({ command: "shutdown" }) }
    function reboot() { send({ command: "reboot" }) }

    WebSocket {
        id: sock
        url: "ws://" + root.host + ":8080"
        active: true
        onStatusChanged: {
            if (sock.status === WebSocket.Closed || sock.status === WebSocket.Error)
                reconnect.start()
        }
        onTextMessageReceived: (message) => {
            var msg
            try { msg = JSON.parse(message) } catch (e) { return }
            if (msg.type === "shutdown_ack") { root.shuttingDown = true; return }
            root.sourcesMuted = msg.sources_muted || {}
            root.sourcesPlaying = msg.sources_playing || {}
            if (msg.channels) root.channels = msg.channels
            root.inputRate = msg.input_rate || 0
            root.btState = msg.bt_state || "idle"
            root.btDevice = msg.bt_device || ""
            root.partyMode = msg.party_mode === true
            root.firstStateReceived = true
            root.newState()
        }
    }
    Timer {
        id: reconnect
        interval: 3000
        onTriggered: { sock.active = false; sock.active = true }
    }
}
