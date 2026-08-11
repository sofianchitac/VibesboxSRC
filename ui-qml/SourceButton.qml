import QtQuick
import Qt5Compat.GraphicalEffects

// Per-source mute-toggle button. States (mirrors ui/style.css):
//   idle   — grey background, dark title
//   lit    — playing & unmuted: white background, accent title, accent glow
//   muted  — background slowly breathes grey <-> accent
//   pairing — Bluetooth only: accent border + pulsing accent glow
// Labels are dropped (v3 decision); `sublabel` carries only the connected
// Bluetooth device name.
Item {
    id: root
    property string title
    property bool lit: false
    property bool muted: false
    property bool pairing: false
    property color accent: Theme.orange
    property string sublabel: ""
    signal tapped()

    // 0..1 driver for the muted grey<->accent breathing
    property real pulse: 0
    onMutedChanged: if (!muted) pulse = 0
    SequentialAnimation on pulse {
        running: root.muted
        loops: Animation.Infinite
        NumberAnimation { to: 1; duration: 2000; easing.type: Easing.InOutSine }
        NumberAnimation { to: 0; duration: 2000; easing.type: Easing.InOutSine }
    }

    // pairing glow pulse (bt-pulse, 1.5s)
    property real pairPulse: 0
    SequentialAnimation on pairPulse {
        running: root.pairing
        loops: Animation.Infinite
        NumberAnimation { to: 1; duration: 750; easing.type: Easing.InOutSine }
        NumberAnimation { to: 0; duration: 750; easing.type: Easing.InOutSine }
    }

    scale: tapArea.pressed ? 0.95 : 1
    Behavior on scale { NumberAnimation { duration: 100 } }

    RectangularGlow {
        anchors.fill: body
        glowRadius: Theme.s(20)
        cornerRadius: body.radius + glowRadius
        color: root.accent
        visible: root.lit || root.pairing
        opacity: root.lit ? 0.9 : root.pairPulse * 0.8
    }

    Rectangle {
        id: body
        anchors.fill: parent
        radius: Theme.s(20)
        border.width: Theme.s(2)
        border.color: root.pairing ? root.accent
                    : root.lit ? "#FFFFFF" : "transparent"
        color: root.muted ? Theme.mix(Theme.grey, root.accent, root.pulse)
             : root.lit ? "#FFFFFF" : Theme.grey
        Behavior on color { enabled: !root.muted; ColorAnimation { duration: 200 } }

        Column {
            anchors.centerIn: parent
            spacing: Theme.s(1)
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.title
                font.family: Theme.fontFamily
                font.pixelSize: Theme.s(30)
                font.weight: Font.Bold
                color: root.lit ? root.accent : Theme.btnText
                Behavior on color { ColorAnimation { duration: 200 } }
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: root.sublabel !== ""
                text: root.sublabel
                font.family: Theme.fontFamily
                font.pixelSize: Theme.s(12)
                font.weight: Font.Medium
                font.letterSpacing: Theme.s(1.5)
                color: root.lit ? root.accent : Theme.btnText
            }
        }
    }

    MouseArea {
        id: tapArea
        anchors.fill: parent
        onClicked: root.tapped()
    }
}
