import QtQuick

// Staggered slide-up entrance (the web UI's .animate-wake). Content starts
// invisible and 30px low; when `awake` flips true it springs into place after
// `wakeDelay` ms.
Item {
    id: root
    property int wakeDelay: 0
    property bool awake: false
    default property alias content: inner.data

    opacity: 0
    transform: Translate { id: slide; y: Theme.s(30) }

    Item { id: inner; anchors.fill: parent }

    onAwakeChanged: if (awake) anim.start()

    SequentialAnimation {
        id: anim
        PauseAnimation { duration: root.wakeDelay }
        ParallelAnimation {
            NumberAnimation {
                target: root; property: "opacity"; to: 1
                duration: 600; easing.type: Easing.OutCubic
            }
            NumberAnimation {
                target: slide; property: "y"; to: 0
                duration: 600; easing.type: Easing.OutBack; easing.overshoot: 1.3
            }
        }
    }
}
