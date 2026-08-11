import QtQuick

// One RMS meter bar. -60..0 dB maps to 0..1 fill; the full gradient is always
// painted and compressed by a bottom-origin y-scale (same trick as the CSS
// scaleY transform — compositor-cheap, no relayout at 10 Hz).
// Two gradient rects (orange / cyan) cross-fade with Theme.colorMode.
//
// Geometry: the bar occupies everything above `reservedBottom` (the parent's
// border line + label zone); the channel label sits centred at the bottom.
Item {
    id: root
    property string label
    property real db: -100
    property real reservedBottom: Theme.s(26)   // border (4) + label zone (22)
    readonly property real level: Math.max(0, Math.min(1, (db + 60) / 60))

    Item {
        id: bar
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.bottomMargin: root.reservedBottom
        width: Math.min(root.width, Theme.s(40))

        transform: Scale {
            origin.y: bar.height
            yScale: root.level
            Behavior on yScale { NumberAnimation { duration: 90 } }
        }

        Rectangle {
            anchors.fill: parent
            radius: Theme.s(3)
            gradient: Gradient {
                GradientStop { position: 0.0; color: Theme.orange }
                GradientStop { position: 0.5; color: Theme.orangeLight }
                GradientStop { position: 1.0; color: "#FFFFFF" }
            }
            opacity: Theme.colorMode ? 0 : 1
            Behavior on opacity { NumberAnimation { duration: 300 } }
        }
        Rectangle {
            anchors.fill: parent
            radius: Theme.s(3)
            gradient: Gradient {
                GradientStop { position: 0.0; color: Theme.grey }
                GradientStop { position: 0.28; color: Theme.orange }
                GradientStop { position: 0.5; color: Theme.orangeLight }
                GradientStop { position: 0.75; color: Theme.cyan }
                GradientStop { position: 1.0; color: "#FFFFFF" }
            }
            opacity: Theme.colorMode ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 300 } }
        }
    }

    Text {
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        text: root.label
        font.family: Theme.fontFamily
        font.pixelSize: Theme.s(11)
        color: Theme.textMuted
    }
}
