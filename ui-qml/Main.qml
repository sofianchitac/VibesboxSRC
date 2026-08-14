import QtQuick
import QtQuick.Window

// Vibesbox SRC touchscreen dashboard — QML port of ui/ (index.html + app.js).
// Runs with the stock qml runtime:  /usr/lib/qt6/bin/qml Main.qml
// Dev against a remote Pi:          qml Main.qml -- --host=vibesbox-src.local
//
// The whole layout is authored in 480x800 design pixels and scaled uniformly
// to the window (Theme.s), so it ports to any resolution unchanged.
Window {
    id: win
    visible: true
    visibility: Window.FullScreen
    color: Theme.bg
    title: "Vibesbox SRC"

    property string host: {
        var args = Qt.application.arguments
        for (var i = 0; i < args.length; i++)
            if (args[i].indexOf("--host=") === 0)
                return args[i].substring(7)
        return "localhost"
    }

    // First state received: hide splash, run the wake animation
    property bool awake: false
    // Local mirror of the last brightness sent (daemon does not report it back)
    property int currentBrightness: 255
    // Black-hole shutdown FX (fires on request, like the web UI)
    property bool shuttingDownFx: false

    readonly property var sourceDefs: [
        { name: "USB", title: "USB" },
        { name: "Lyrion", title: "Squeeze" },
        { name: "AirPlay", title: "AirPlay" },
        { name: "Bluetooth", title: "Bluetooth" },
        { name: "Tidal", title: "Tidal" },
        { name: "TV", title: "TV" },
    ]
    // The bus is 8 wide since 2026-08-13; indices past this list fall through to the
    // "Ch7"/"Ch8" fallback below, which is DELIBERATE. Naming lanes 7-8 would assert a
    // speaker mapping, and mapping is REAPER's alone — a neutral label is the honest one.
    readonly property var channelLabels: router.channels > 2
        ? ["FL", "FR", "RL", "RR", "FC", "LFE"]   // fixed physical USB 5.1 order
        : ["L", "R"]
    readonly property bool anyMuted: {
        var m = router.sourcesMuted
        for (var k in m) if (m[k]) return true
        return false
    }

    // Under eglfs there is no compositor rotating the output: the DSI panel
    // scans out native 800x480 landscape, so the portrait canvas is rotated
    // into it in-app (90 = the same mapping as sway's `output * transform 90`).
    // Touch needs no calibration matrix — Qt maps input through the item
    // transform. Under wayland/desktop the window is already portrait.
    readonly property bool rotated: Qt.platform.pluginName === "eglfs"

    Binding {
        target: Theme
        property: "scale"
        value: win.rotated
               ? Math.min(win.height / Theme.designWidth, win.width / Theme.designHeight)
               : Math.min(win.width / Theme.designWidth, win.height / Theme.designHeight)
    }

    RouterClient { id: router; host: win.host }
    CdspClient { id: cdsp; host: win.host }

    // Rising-edge detection: meter thump when a source joins the mix
    property var prevLit: ({})
    Connections {
        target: router
        function onNewState() {
            var lit = {}
            for (var i = 0; i < win.sourceDefs.length; i++) {
                var name = win.sourceDefs[i].name
                var muted = router.sourcesMuted[name] === true
                var playing = name === "Bluetooth" ? router.btState === "playing"
                                                   : router.sourcesPlaying[name] === true
                lit[name] = playing && !muted
                if (win.awake && lit[name] && !win.prevLit[name])
                    meterBank.thump()
            }
            win.prevLit = lit
            win.awake = true
        }
    }

    // ── App canvas (480x800 design space, centred) ─────────────────────────
    Item {
        id: canvas
        width: Theme.s(Theme.designWidth)
        height: Theme.s(Theme.designHeight)
        anchors.centerIn: parent
        rotation: win.rotated ? 90 : 0

        Item {
            id: app
            anchors.fill: parent
            anchors.margins: Theme.s(40)

            transform: Scale {
                id: blackHole
                origin.x: app.width / 2
                origin.y: app.height / 2
            }
            SequentialAnimation {
                id: blackHoleAnim
                ParallelAnimation {
                    NumberAnimation { target: blackHole; property: "xScale"; to: 0; duration: 1500; easing.type: Easing.InQuad }
                    NumberAnimation { target: blackHole; property: "yScale"; to: 0; duration: 1500; easing.type: Easing.InQuad }
                    NumberAnimation { target: app; property: "opacity"; to: 0; duration: 1500; easing.type: Easing.InQuad }
                }
            }

            // Header logo — tap toggles color mode (DSP LogoBadge behaviour)
            WakeSection {
                id: header
                awake: win.awake
                wakeDelay: 100
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: Theme.s(28)

                Item {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: Theme.s(240)
                    height: parent.height
                    Image {
                        id: greyLogo
                        anchors.centerIn: parent
                        width: parent.width
                        fillMode: Image.PreserveAspectFit
                        source: "../ui/vibesbox-src-logo-grey.svg"
                        sourceSize.width: width * 2
                        opacity: Theme.colorMode ? 0 : 1
                        Behavior on opacity { NumberAnimation { duration: 300; easing.type: Easing.InOutSine } }
                    }
                    Image {
                        id: colorLogo
                        anchors.centerIn: parent
                        width: parent.width
                        fillMode: Image.PreserveAspectFit
                        source: "../ui/vibesbox-src-logo.svg"
                        sourceSize.width: width * 2
                        opacity: Theme.colorMode ? 1 : 0
                        Behavior on opacity { NumberAnimation { duration: 300; easing.type: Easing.InOutSine } }
                    }
                    MouseArea {
                        // generous touch target around the slim wordmark
                        anchors.fill: parent
                        anchors.margins: -Theme.s(10)
                        onClicked: Theme.colorMode = !Theme.colorMode
                    }
                }
            }

            // Stats row
            WakeSection {
                id: statsSection
                awake: win.awake
                wakeDelay: 200
                anchors.top: header.bottom
                anchors.topMargin: Theme.s(36)
                anchors.left: parent.left
                anchors.right: parent.right
                height: Theme.s(72)

                Row {
                    anchors.fill: parent
                    StatColumn {
                        width: parent.width / 3
                        label: "INPUT"
                        value: router.inputRate > 0
                               ? String(parseFloat((router.inputRate / 1000).toFixed(1))) : "—"
                        unit: router.inputRate > 0 ? "kHz" : ""
                    }
                    StatColumn {
                        width: parent.width / 3
                        label: "RESAMPLER"
                        value: "96"
                        unit: "kHz"
                        glow: true
                    }
                    StatColumn {
                        width: parent.width / 3
                        label: "OUTPUT"
                        value: "NDI"
                        glow: true
                    }
                }
            }

            // Level meters
            WakeSection {
                id: metersSection
                awake: win.awake
                wakeDelay: 300
                anchors.top: statsSection.bottom
                anchors.topMargin: Theme.s(24)
                anchors.left: parent.left
                anchors.right: parent.right
                height: Theme.s(182)   // 156 bars + 4 border + 22 labels

                Item {
                    id: meterBank
                    anchors.fill: parent

                    function thump() { thumpAnim.restart() }
                    SequentialAnimation {
                        id: thumpAnim
                        NumberAnimation { target: meterBank; property: "scale"; to: 1.08; duration: 160; easing.type: Easing.OutQuad }
                        NumberAnimation { target: meterBank; property: "scale"; to: 1; duration: 240; easing.type: Easing.OutBack; easing.overshoot: 1.3 }
                    }

                    Row {
                        anchors.fill: parent
                        Repeater {
                            model: router.channels
                            MeterChannel {
                                width: meterBank.width / router.channels
                                height: meterBank.height
                                label: win.channelLabels[index] || ("Ch" + (index + 1))
                                db: cdsp.levels.length > index && cdsp.levels[index] != null
                                    ? cdsp.levels[index] : -100
                            }
                        }
                    }
                    Rectangle {
                        // baseline the bars sit on
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: Theme.s(22)
                        height: Theme.s(4)
                        color: Theme.textPrimary
                    }
                }
            }

            // Source buttons, 2x3 — labels dropped (v3): state is the colour
            WakeSection {
                id: gridSection
                awake: win.awake
                wakeDelay: 400
                anchors.top: metersSection.bottom
                anchors.topMargin: Theme.s(20)
                anchors.left: parent.left
                anchors.right: parent.right
                height: grid.height

                Grid {
                    id: grid
                    columns: 2
                    columnSpacing: Theme.s(20)
                    rowSpacing: Theme.s(20)
                    width: parent.width
                    Repeater {
                        model: win.sourceDefs
                        SourceButton {
                            readonly property string src: modelData.name
                            readonly property bool isBt: src === "Bluetooth"
                            width: (grid.width - grid.columnSpacing) / 2
                            height: Theme.s(76)
                            title: modelData.title
                            accent: isBt ? Theme.cyan : Theme.orange
                            muted: router.sourcesMuted[src] === true
                            lit: !muted && (isBt ? router.btState === "playing"
                                                 : router.sourcesPlaying[src] === true)
                            pairing: isBt && !muted && !lit && router.btState === "pairing"
                            sublabel: isBt && router.btDevice !== ""
                                      && (router.btState === "playing" || router.btState === "paired")
                                      ? router.btDevice.toUpperCase() : ""
                            onTapped: router.toggleMute(src)
                        }
                    }
                }
            }

            // Footer
            WakeSection {
                id: footer
                awake: win.awake
                wakeDelay: 500
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: Theme.s(44)

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left
                    text: "Unmute All"
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.s(30)
                    font.weight: Font.Bold
                    color: win.anyMuted ? Theme.orange : Theme.darkGrey
                    Behavior on color { ColorAnimation { duration: 200 } }
                    scale: unmuteTap.pressed ? 0.95 : 1
                    MouseArea {
                        id: unmuteTap
                        anchors.fill: parent
                        anchors.margins: -Theme.s(8)
                        onClicked: router.unmuteAll()
                    }
                }

                Rectangle {
                    id: powerBtn
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.right: parent.right
                    width: Theme.s(44); height: Theme.s(44)
                    radius: width / 2
                    color: Theme.orange
                    opacity: router.shuttingDown ? 0.5 : 1
                    scale: powerTap.pressed ? 0.92 : 1
                    Behavior on scale { NumberAnimation { duration: 100 } }
                    Image {
                        anchors.centerIn: parent
                        source: "icons/power.svg"
                        width: Theme.s(24); height: Theme.s(24)
                        sourceSize: Qt.size(width * 2, height * 2)
                    }
                    MouseArea {
                        id: powerTap
                        anchors.fill: parent
                        enabled: !router.shuttingDown
                        onClicked: powerOverlay.open(win.currentBrightness)
                    }
                }
            }
        }

        // Power menu overlay
        PowerOverlay {
            id: powerOverlay
            anchors.fill: parent
            z: 150
            onBrightnessMoved: (level) => {
                win.currentBrightness = level
                router.setBrightness(level)
            }
            onShutdownRequested: {
                shown = false
                win.shuttingDownFx = true
                blackHoleAnim.start()
                router.shutdown()
            }
            onRebootRequested: {
                shown = false
                win.shuttingDownFx = true
                blackHoleAnim.start()
                router.reboot()
            }
        }

        // Shutdown message (after the black hole)
        Item {
            anchors.fill: parent
            z: 200
            visible: opacity > 0
            opacity: win.shuttingDownFx ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 1000; easing.type: Easing.InOutSine } }
            Column {
                anchors.centerIn: parent
                width: parent.width * 0.8
                spacing: Theme.s(24)
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Shutting down..."
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.s(24)
                    font.weight: Font.Bold
                    color: Theme.textPrimary
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: parent.width
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    text: "Please power off the device from the physical button."
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.s(18)
                    color: Theme.textMuted
                }
            }
        }

        // Splash — breathes until the first daemon state arrives
        Rectangle {
            id: splash
            anchors.fill: parent
            z: 300
            color: Theme.bg
            visible: opacity > 0
            opacity: win.awake ? 0 : 1
            Behavior on opacity { NumberAnimation { duration: 500 } }
            Image {
                id: splashLogo
                anchors.centerIn: parent
                width: Theme.s(320)
                fillMode: Image.PreserveAspectFit
                source: "../ui/vibesbox-src-logo.svg"
                sourceSize.width: width * 2
                SequentialAnimation on scale {
                    running: splash.visible
                    loops: Animation.Infinite
                    NumberAnimation { to: 1.05; duration: 1250; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.0; duration: 1250; easing.type: Easing.InOutSine }
                }
            }
        }
    }
}
