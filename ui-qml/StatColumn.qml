import QtQuick
import Qt5Compat.GraphicalEffects

// One column of the INPUT / RESAMPLER / OUTPUT stats row. The glow variant
// (RESAMPLER / OUTPUT) gets a soft procedural halo behind the value — the CSS
// text-shadow equivalent (RectangularGlow paints beyond bounds; texture-based
// Glow would clip at the text rect).
Column {
    id: root
    property string label
    property string value
    property string unit: ""
    property bool glow: false

    spacing: Theme.s(4)

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: root.label
        font.family: Theme.fontFamily
        font.pixelSize: Theme.s(12)
        font.letterSpacing: Theme.s(1.5)
        color: Theme.textMuted
    }

    Item {
        anchors.horizontalCenter: parent.horizontalCenter
        width: valueText.width + (unitText.visible ? unitText.width + Theme.s(3) : 0)
        height: valueText.height

        RectangularGlow {
            anchors.fill: parent
            anchors.topMargin: Theme.s(8)
            anchors.bottomMargin: Theme.s(8)
            glowRadius: Theme.s(22)
            cornerRadius: Theme.s(22)
            color: Theme.orange
            opacity: 0.35
            visible: root.glow
        }
        Text {
            id: valueText
            text: root.value
            font.family: Theme.fontFamily
            font.pixelSize: Theme.s(40)
            font.weight: Font.Bold
            color: root.glow ? Theme.orangeLight : Theme.textPrimary
        }
        Text {
            id: unitText
            visible: root.unit !== ""
            anchors.baseline: valueText.baseline
            anchors.left: valueText.right
            anchors.leftMargin: Theme.s(3)
            text: root.unit
            font.family: Theme.fontFamily
            font.pixelSize: Theme.s(16)
            color: root.glow ? Theme.orangeLight : Theme.textPrimary
        }
    }
}
