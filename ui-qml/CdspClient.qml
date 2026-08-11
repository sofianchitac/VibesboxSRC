import QtQuick
import QtWebSockets

// CamillaDSP native WebSocket (:1234) — polls playback RMS at 10 Hz.
Item {
    id: root
    property string host: "localhost"
    property var levels: []    // dB per channel

    WebSocket {
        id: sock
        url: "ws://" + root.host + ":1234"
        active: true
        onStatusChanged: {
            if (sock.status === WebSocket.Closed || sock.status === WebSocket.Error)
                reconnect.start()
        }
        onTextMessageReceived: (message) => {
            var msg
            try { msg = JSON.parse(message) } catch (e) { return }
            var r = msg.GetPlaybackSignalRms
            if (r && r.result === "Ok" && Array.isArray(r.value))
                root.levels = r.value
        }
    }
    Timer {
        interval: 100; repeat: true
        running: sock.status === WebSocket.Open
        onTriggered: sock.sendTextMessage('{"GetPlaybackSignalRms": null}')
    }
    Timer {
        id: reconnect
        interval: 3000
        onTriggered: { sock.active = false; sock.active = true }
    }
}
