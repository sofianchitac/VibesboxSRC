pragma Singleton
import QtQuick

// Design tokens (mirrors ui/Design.md). All component dimensions are authored
// in 480x800 "design pixels" and passed through s(); Main sets `scale` from the
// real window size, so the whole UI ports to any resolution unchanged.
QtObject {
    readonly property real designWidth: 480
    readonly property real designHeight: 800
    property real scale: 1
    function s(px) { return px * scale }

    // Palette
    readonly property color bg: "#000000"
    readonly property color textPrimary: "#FFFFFF"
    readonly property color textMuted: "#8B796B"
    readonly property color orange: "#FF7049"
    readonly property color orangeLight: "#FDA564"
    readonly property color grey: "#8B796B"
    readonly property color darkGrey: "#141414"
    readonly property color cyan: "#59C2EB"
    readonly property color cyanLight: "#A8DFF5"
    readonly property color btnText: "#111111"

    // Logo-tap "color mode" (mirrors the DSP kiosk LogoBadge): color logo on,
    // RMS meters switch from the orange gradient to the cyan one.
    property bool colorMode: false

    // Qt 6.4 renders only a variable font's default instance and its declared
    // weight range shadows any static face in the same family — so the variable
    // TTF from ui/ is NOT loaded; all three weights are pre-instanced static
    // TTFs (fonttools varLib.instancer). font.weight then matches correctly.
    readonly property FontLoader instrumentSans: FontLoader {
        source: Qt.resolvedUrl("fonts/InstrumentSans-Regular.ttf")
    }
    readonly property FontLoader instrumentSansMedium: FontLoader {
        source: Qt.resolvedUrl("fonts/InstrumentSans-Medium.ttf")
    }
    readonly property FontLoader instrumentSansBold: FontLoader {
        source: Qt.resolvedUrl("fonts/InstrumentSans-Bold.ttf")
    }
    readonly property string fontFamily: instrumentSans.status === FontLoader.Ready
                                         ? instrumentSans.name : "sans-serif"

    function mix(a, b, t) {
        return Qt.rgba(a.r + (b.r - a.r) * t,
                       a.g + (b.g - a.g) * t,
                       a.b + (b.b - a.b) * t, 1)
    }
}
