import QtQuick
import Qt5Compat.GraphicalEffects

// Power menu: Shutdown / Reboot circular actions + display brightness slider.
Item {
    id: root
    property bool shown: false
    property real brightness: 255      // set by open(); live while dragging
    signal shutdownRequested()
    signal rebootRequested()
    signal brightnessMoved(int level)

    function open(currentBrightness) {
        brightness = currentBrightness
        shown = true
    }

    visible: opacity > 0
    opacity: shown ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 200 } }

    // backdrop — tap to close
    Rectangle {
        anchors.fill: parent
        color: "#000000"
        opacity: 0.8
        MouseArea { anchors.fill: parent; onClicked: root.shown = false }
    }

    Rectangle {
        id: card
        anchors.centerIn: parent
        width: Theme.s(400)
        height: column.height + Theme.s(72)
        radius: Theme.s(24)
        color: Theme.darkGrey
        MouseArea { anchors.fill: parent }   // swallow taps

        Column {
            id: column
            anchors.centerIn: parent
            width: card.width - Theme.s(64)
            spacing: Theme.s(36)

            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.s(64)

                // Shutdown
                Column {
                    spacing: Theme.s(14)
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: Theme.s(72); height: Theme.s(72)
                        radius: width / 2
                        color: Theme.orange
                        opacity: shutdownTap.pressed ? 0.7 : 1
                        Image {
                            anchors.centerIn: parent
                            source: "icons/power.svg"
                            width: Theme.s(28); height: Theme.s(28)
                            sourceSize: Qt.size(width * 2, height * 2)
                        }
                        MouseArea {
                            id: shutdownTap
                            anchors.fill: parent
                            onClicked: root.shutdownRequested()
                        }
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Shutdown"
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.s(14)
                        font.weight: Font.Bold
                        font.letterSpacing: Theme.s(0.5)
                        color: Theme.textPrimary
                    }
                }

                // Reboot
                Column {
                    spacing: Theme.s(14)
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: Theme.s(72); height: Theme.s(72)
                        radius: width / 2
                        color: Theme.cyan
                        opacity: rebootTap.pressed ? 0.7 : 1
                        Image {
                            anchors.centerIn: parent
                            source: "icons/reboot.svg"
                            width: Theme.s(28); height: Theme.s(28)
                            sourceSize: Qt.size(width * 2, height * 2)
                        }
                        MouseArea {
                            id: rebootTap
                            anchors.fill: parent
                            onClicked: root.rebootRequested()
                        }
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Reboot"
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.s(14)
                        font.weight: Font.Bold
                        font.letterSpacing: Theme.s(0.5)
                        color: Theme.textPrimary
                    }
                }
            }

            // Brightness
            Column {
                width: parent.width
                spacing: Theme.s(14)
                Text {
                    text: "DISPLAY"
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.s(12)
                    font.letterSpacing: Theme.s(1.5)
                    color: Theme.textMuted
                }
                Item {
                    id: slider
                    width: parent.width
                    height: Theme.s(30)
                    readonly property real handleR: Theme.s(15)
                    readonly property real usable: width - handleR * 2

                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width
                        height: Theme.s(6)
                        radius: Theme.s(3)
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0; color: "#1a1a1a" }
                            GradientStop { position: 1.0; color: Theme.orange }
                        }
                    }
                    Rectangle {
                        id: handle
                        anchors.verticalCenter: parent.verticalCenter
                        x: (root.brightness / 255) * slider.usable
                        width: slider.handleR * 2
                        height: slider.handleR * 2
                        radius: slider.handleR
                        color: Theme.orange
                    }
                    RectangularGlow {
                        anchors.fill: handle
                        glowRadius: Theme.s(10)
                        cornerRadius: handle.radius + glowRadius
                        color: Theme.orange
                        opacity: 0.6
                        z: -1
                    }
                    MouseArea {
                        anchors.fill: parent
                        function apply(mx) {
                            var t = Math.max(0, Math.min(1, (mx - slider.handleR) / slider.usable))
                            root.brightness = Math.round(t * 255)
                            root.brightnessMoved(root.brightness)
                        }
                        onPressed: (mouse) => apply(mouse.x)
                        onPositionChanged: (mouse) => apply(mouse.x)
                    }
                }
            }
        }
    }
}
