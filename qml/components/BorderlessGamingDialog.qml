import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Basic 2.15 as Basic
import QtQuick.Layouts 1.15

Dialog {
    id: dlg
    title: "Borderless Gaming & Mouse Capture"
    modal: true
    anchors.centerIn: parent
    width: 680
    height: 620

    // State
    property var windowList: []
    property var gameCompat: []
    property int selectedHwnd: 0
    property string selectedTitle: ""
    property bool cursorReleaseOn: false
    property bool borderlessApplied: false
    property string autoDetectedGame: ""
    property string statusMessage: ""
    property int cursorReleaseInterval: 2

    // Tab index: 0 = Game Setup, 1 = Compatibility
    property int currentTab: 0

    background: Rectangle { color: "#1e1e1e"; border.color: "#555"; border.width: 1; radius: 8 }

    header: Rectangle {
        color: "#2a2a2a"
        height: 48
        radius: 8

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16

            Label {
                text: "Borderless Gaming"
                color: "white"
                font.pixelSize: 18
                font.bold: true
                Layout.fillWidth: true
            }

            Label {
                text: dlg.cursorReleaseOn ? "CURSOR FREE" : ""
                color: "#00cc44"
                font.pixelSize: 12
                font.bold: true
                visible: dlg.cursorReleaseOn
            }
        }
    }

    contentItem: Column {
        spacing: 0

        // Tab bar
        Row {
            width: parent.width
            height: 36

            Repeater {
                model: ["Game Setup", "Game Compatibility"]
                delegate: Rectangle {
                    width: parent.width / 2
                    height: 36
                    color: dlg.currentTab === index ? "#333" : "#252525"
                    border.color: dlg.currentTab === index ? "#4a9eff" : "#444"
                    border.width: dlg.currentTab === index ? 2 : 0

                    Label {
                        anchors.centerIn: parent
                        text: modelData
                        color: dlg.currentTab === index ? "#4a9eff" : "#aaa"
                        font.pixelSize: 13
                        font.bold: dlg.currentTab === index
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: dlg.currentTab = index
                    }
                }
            }
        }

        // Tab content
        StackLayout {
            width: parent.width
            height: parent.height - 36
            currentIndex: dlg.currentTab

            // ===== Tab 0: Game Setup =====
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                Flickable {
                    anchors.fill: parent
                    anchors.margins: 12
                    contentHeight: setupCol.implicitHeight + 20
                    clip: true
                    flickableDirection: Flickable.VerticalFlick
                    Basic.ScrollBar.vertical: Basic.ScrollBar { policy: Basic.ScrollBar.AsNeeded }

                    Column {
                        id: setupCol
                        width: parent.width
                        spacing: 14

                        // Auto-detect banner
                        Rectangle {
                            width: parent.width
                            height: autoDetectCol.implicitHeight + 16
                            color: dlg.autoDetectedGame !== "" ? "#1a3a1a" : "#2a2a2a"
                            border.color: dlg.autoDetectedGame !== "" ? "#00aa44" : "#444"
                            border.width: 1
                            radius: 6

                            Column {
                                id: autoDetectCol
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 6

                                Row {
                                    spacing: 8
                                    Label {
                                        text: "Auto-Detect"
                                        color: "#4a9eff"
                                        font.pixelSize: 13
                                        font.bold: true
                                    }
                                    Label {
                                        text: dlg.autoDetectedGame !== "" ? ("Found: " + dlg.autoDetectedGame) : "No known game detected"
                                        color: dlg.autoDetectedGame !== "" ? "#00cc44" : "#888"
                                        font.pixelSize: 12
                                    }
                                }

                                Basic.Button {
                                    text: "Scan for Games"
                                    onClicked: dlg._autoDetect()
                                    contentItem: Label {
                                        text: parent.text
                                        color: "white"
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                        font.pixelSize: 12
                                    }
                                    background: Rectangle {
                                        implicitWidth: 130
                                        implicitHeight: 28
                                        color: parent.down ? "#2563eb" : (parent.hovered ? "#3a7aef" : "#3b82f6")
                                        radius: 4
                                    }
                                }
                            }
                        }

                        // Window picker
                        Label {
                            text: "Select Game Window"
                            color: "#ccc"
                            font.pixelSize: 14
                            font.bold: true
                        }

                        Row {
                            spacing: 8
                            width: parent.width

                            Basic.ComboBox {
                                id: windowCombo
                                width: parent.width - refreshBtn.width - 8
                                model: {
                                    var titles = []
                                    for (var i = 0; i < dlg.windowList.length; i++) {
                                        var w = dlg.windowList[i]
                                        var suffix = w.isBorderless ? " [borderless]" : ""
                                        titles.push(w.title + " (" + w.width + "x" + w.height + ")" + suffix)
                                    }
                                    return titles
                                }
                                onCurrentIndexChanged: {
                                    if (currentIndex >= 0 && currentIndex < dlg.windowList.length) {
                                        dlg.selectedHwnd = dlg.windowList[currentIndex].hwnd
                                        dlg.selectedTitle = dlg.windowList[currentIndex].title
                                        dlg.borderlessApplied = dlg.windowList[currentIndex].isBorderless
                                    }
                                }

                                background: Rectangle {
                                    implicitHeight: 32
                                    color: "#2a2a2a"
                                    border.color: windowCombo.activeFocus ? "#4a9eff" : "#555"
                                    border.width: 1
                                    radius: 4
                                }
                                contentItem: Label {
                                    text: windowCombo.displayText
                                    color: "white"
                                    font.pixelSize: 12
                                    verticalAlignment: Text.AlignVCenter
                                    leftPadding: 8
                                    elide: Text.ElideRight
                                }
                                popup: Popup {
                                    y: windowCombo.height
                                    width: windowCombo.width
                                    implicitHeight: Math.min(contentItem.implicitHeight + 2, 300)
                                    padding: 1
                                    background: Rectangle { color: "#2a2a2a"; border.color: "#555"; radius: 4 }
                                    contentItem: ListView {
                                        clip: true
                                        implicitHeight: contentHeight
                                        model: windowCombo.popup.visible ? windowCombo.delegateModel : null
                                        currentIndex: windowCombo.highlightedIndex
                                        Basic.ScrollBar.vertical: Basic.ScrollBar { policy: Basic.ScrollBar.AsNeeded }
                                    }
                                }
                                delegate: ItemDelegate {
                                    width: windowCombo.width
                                    height: 30
                                    contentItem: Label {
                                        text: modelData
                                        color: parent.highlighted ? "white" : "#ccc"
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    background: Rectangle {
                                        color: parent.highlighted ? "#3b82f6" : "transparent"
                                    }
                                    highlighted: windowCombo.highlightedIndex === index
                                }
                            }

                            Basic.Button {
                                id: refreshBtn
                                text: "Refresh"
                                onClicked: dlg._refreshWindows()
                                contentItem: Label {
                                    text: parent.text
                                    color: "white"
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    font.pixelSize: 12
                                }
                                background: Rectangle {
                                    implicitWidth: 70
                                    implicitHeight: 32
                                    color: parent.down ? "#444" : (parent.hovered ? "#3a3a3a" : "#333")
                                    border.color: "#555"
                                    border.width: 1
                                    radius: 4
                                }
                            }
                        }

                        // Window Position & Size
                        Rectangle {
                            width: parent.width
                            height: geomCol.implicitHeight + 20
                            color: "#252525"
                            border.color: "#444"
                            border.width: 1
                            radius: 6

                            Column {
                                id: geomCol
                                anchors.fill: parent
                                anchors.margins: 10
                                spacing: 8

                                Row {
                                    spacing: 8
                                    Label {
                                        text: "Window Position & Size"
                                        color: "#ccc"
                                        font.pixelSize: 14
                                        font.bold: true
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                    Basic.Button {
                                        text: "Use Current"
                                        enabled: dlg.selectedHwnd > 0
                                        onClicked: {
                                            // Pre-fill from selected window's current geometry
                                            for (var i = 0; i < dlg.windowList.length; i++) {
                                                if (dlg.windowList[i].hwnd === dlg.selectedHwnd) {
                                                    winX.text = "" + dlg.windowList[i].x
                                                    winY.text = "" + dlg.windowList[i].y
                                                    winW.text = "" + dlg.windowList[i].width
                                                    winH.text = "" + dlg.windowList[i].height
                                                    break
                                                }
                                            }
                                        }
                                        contentItem: Label {
                                            text: parent.text; color: parent.enabled ? "#4a9eff" : "#666"
                                            font.pixelSize: 11; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                        }
                                        background: Rectangle {
                                            implicitWidth: 80; implicitHeight: 24
                                            color: parent.down ? "#333" : "transparent"
                                            border.color: parent.enabled ? "#4a9eff" : "#555"; border.width: 1; radius: 4
                                        }
                                    }
                                }

                                // X, Y row
                                Row {
                                    spacing: 12
                                    Label { text: "X:"; color: "#aaa"; font.pixelSize: 12; width: 14; anchors.verticalCenter: parent.verticalCenter }
                                    Basic.TextField {
                                        id: winX; width: 70; text: "0"
                                        color: "white"; font.pixelSize: 12
                                        validator: IntValidator { bottom: -9999; top: 99999 }
                                        background: Rectangle { color: "#1e1e1e"; border.color: winX.activeFocus ? "#4a9eff" : "#555"; border.width: 1; radius: 3 }
                                    }
                                    Label { text: "Y:"; color: "#aaa"; font.pixelSize: 12; width: 14; anchors.verticalCenter: parent.verticalCenter }
                                    Basic.TextField {
                                        id: winY; width: 70; text: "0"
                                        color: "white"; font.pixelSize: 12
                                        validator: IntValidator { bottom: -9999; top: 99999 }
                                        background: Rectangle { color: "#1e1e1e"; border.color: winY.activeFocus ? "#4a9eff" : "#555"; border.width: 1; radius: 3 }
                                    }
                                    Label { text: "W:"; color: "#aaa"; font.pixelSize: 12; width: 18; anchors.verticalCenter: parent.verticalCenter }
                                    Basic.TextField {
                                        id: winW; width: 70; text: "1280"
                                        color: "white"; font.pixelSize: 12
                                        validator: IntValidator { bottom: 100; top: 99999 }
                                        background: Rectangle { color: "#1e1e1e"; border.color: winW.activeFocus ? "#4a9eff" : "#555"; border.width: 1; radius: 3 }
                                    }
                                    Label { text: "H:"; color: "#aaa"; font.pixelSize: 12; width: 14; anchors.verticalCenter: parent.verticalCenter }
                                    Basic.TextField {
                                        id: winH; width: 70; text: "720"
                                        color: "white"; font.pixelSize: 12
                                        validator: IntValidator { bottom: 100; top: 99999 }
                                        background: Rectangle { color: "#1e1e1e"; border.color: winH.activeFocus ? "#4a9eff" : "#555"; border.width: 1; radius: 3 }
                                    }
                                }
                            }
                        }

                        // Actions
                        Rectangle {
                            width: parent.width
                            height: actionsCol.implicitHeight + 20
                            color: "#252525"
                            border.color: "#444"
                            border.width: 1
                            radius: 6

                            Column {
                                id: actionsCol
                                anchors.fill: parent
                                anchors.margins: 10
                                spacing: 10

                                Label {
                                    text: "Actions"
                                    color: "#ccc"
                                    font.pixelSize: 14
                                    font.bold: true
                                }

                                // Main action: borderless at custom size + free cursor
                                Basic.Button {
                                    width: parent.width
                                    enabled: dlg.selectedHwnd > 0 && !dlg.borderlessApplied
                                    text: "Apply Borderless at Custom Size + Free Cursor"
                                    onClicked: {
                                        var px = parseInt(winX.text) || 0
                                        var py = parseInt(winY.text) || 0
                                        var pw = parseInt(winW.text) || 1280
                                        var ph = parseInt(winH.text) || 720
                                        var ok = controller.makeGameBorderlessAt(dlg.selectedHwnd, px, py, pw, ph)
                                        if (ok) {
                                            controller.startCursorReleaseWithHwnd(dlg.cursorReleaseInterval, dlg.selectedHwnd)
                                            dlg.borderlessApplied = true
                                            dlg.cursorReleaseOn = true
                                            dlg.statusMessage = "Borderless at " + pw + "x" + ph + " + cursor free"
                                        } else {
                                            dlg.statusMessage = "Failed to apply borderless mode"
                                        }
                                    }
                                    contentItem: Label {
                                        text: parent.text; color: parent.enabled ? "white" : "#666"
                                        font.pixelSize: 13; font.bold: true
                                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                    }
                                    background: Rectangle {
                                        implicitHeight: 40
                                        color: parent.enabled ? (parent.down ? "#15803d" : (parent.hovered ? "#22a050" : "#16a34a")) : "#333"
                                        radius: 6
                                    }
                                }

                                Row {
                                    spacing: 8
                                    width: parent.width

                                    // Fill Screen variant
                                    Basic.Button {
                                        width: (parent.width - 16) / 3
                                        enabled: dlg.selectedHwnd > 0 && !dlg.borderlessApplied
                                        text: "Fill Screen"
                                        onClicked: {
                                            var ok = controller.makeGameBorderlessFill(dlg.selectedHwnd)
                                            if (ok) {
                                                controller.startCursorReleaseWithHwnd(dlg.cursorReleaseInterval, dlg.selectedHwnd)
                                                dlg.borderlessApplied = true
                                                dlg.cursorReleaseOn = true
                                                dlg.statusMessage = "Borderless fullscreen + cursor free"
                                            } else {
                                                dlg.statusMessage = "Failed"
                                            }
                                        }
                                        contentItem: Label {
                                            text: parent.text; color: parent.enabled ? "white" : "#666"
                                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12
                                        }
                                        background: Rectangle {
                                            implicitHeight: 32
                                            color: parent.enabled ? (parent.down ? "#2563eb" : "#3b82f6") : "#333"
                                            radius: 4
                                        }
                                    }

                                    // Resize only (no borderless toggle)
                                    Basic.Button {
                                        width: (parent.width - 16) / 3
                                        enabled: dlg.selectedHwnd > 0
                                        text: "Move/Resize Only"
                                        onClicked: {
                                            var px = parseInt(winX.text) || 0
                                            var py = parseInt(winY.text) || 0
                                            var pw = parseInt(winW.text) || 1280
                                            var ph = parseInt(winH.text) || 720
                                            var ok = controller.resizeGameWindow(dlg.selectedHwnd, px, py, pw, ph)
                                            dlg.statusMessage = ok ? "Window moved to " + pw + "x" + ph : "Failed"
                                        }
                                        contentItem: Label {
                                            text: parent.text; color: parent.enabled ? "white" : "#666"
                                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12
                                        }
                                        background: Rectangle {
                                            implicitHeight: 32
                                            color: parent.enabled ? (parent.down ? "#444" : "#333") : "#2a2a2a"
                                            border.color: "#555"; border.width: 1; radius: 4
                                        }
                                    }

                                    // Restore
                                    Basic.Button {
                                        width: (parent.width - 16) / 3
                                        enabled: dlg.selectedHwnd > 0 && dlg.borderlessApplied
                                        text: "Restore & Stop"
                                        onClicked: {
                                            controller.restoreAndStopRelease(dlg.selectedHwnd)
                                            dlg.borderlessApplied = false
                                            dlg.cursorReleaseOn = false
                                            dlg.statusMessage = "Window restored"
                                        }
                                        contentItem: Label {
                                            text: parent.text; color: parent.enabled ? "white" : "#666"
                                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12
                                        }
                                        background: Rectangle {
                                            implicitHeight: 32
                                            color: parent.enabled ? (parent.down ? "#7a2020" : "#8b2020") : "#333"
                                            radius: 4
                                        }
                                    }
                                }

                                // Separator
                                Rectangle { width: parent.width; height: 1; color: "#444" }

                                Row {
                                    spacing: 8
                                    width: parent.width

                                    Basic.Button {
                                        width: (parent.width - 8) / 2
                                        enabled: !dlg.cursorReleaseOn
                                        text: "Free Cursor"
                                        onClicked: {
                                            controller.startCursorReleaseWithHwnd(dlg.cursorReleaseInterval, dlg.selectedHwnd)
                                            dlg.cursorReleaseOn = true
                                            dlg.statusMessage = "Cursor release active"
                                        }
                                        contentItem: Label {
                                            text: parent.text; color: parent.enabled ? "white" : "#666"
                                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12
                                        }
                                        background: Rectangle {
                                            implicitHeight: 32
                                            color: parent.enabled ? (parent.down ? "#15803d" : "#16a34a") : "#333"
                                            radius: 4
                                        }
                                    }

                                    Basic.Button {
                                        width: (parent.width - 8) / 2
                                        enabled: dlg.cursorReleaseOn
                                        text: "Stop Cursor Release"
                                        onClicked: {
                                            controller.stopCursorRelease()
                                            dlg.cursorReleaseOn = false
                                            dlg.statusMessage = "Cursor release stopped"
                                        }
                                        contentItem: Label {
                                            text: parent.text; color: parent.enabled ? "white" : "#666"
                                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12
                                        }
                                        background: Rectangle {
                                            implicitHeight: 32
                                            color: parent.enabled ? (parent.down ? "#7a2020" : "#8b2020") : "#333"
                                            radius: 4
                                        }
                                    }
                                }
                            }
                        }

                        // Release interval setting
                        Row {
                            spacing: 10
                            Label {
                                text: "Cursor Release Speed:"
                                color: "#aaa"
                                font.pixelSize: 12
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Basic.Slider {
                                id: intervalSlider
                                width: 200
                                from: 1
                                to: 200
                                stepSize: 1
                                value: dlg.cursorReleaseInterval
                                onValueChanged: dlg.cursorReleaseInterval = Math.round(value)

                                background: Rectangle {
                                    x: intervalSlider.leftPadding
                                    y: intervalSlider.topPadding + intervalSlider.availableHeight / 2 - height / 2
                                    width: intervalSlider.availableWidth
                                    height: 4
                                    radius: 2
                                    color: "#444"
                                    Rectangle {
                                        width: intervalSlider.visualPosition * parent.width
                                        height: parent.height
                                        color: "#4a9eff"
                                        radius: 2
                                    }
                                }
                                handle: Rectangle {
                                    x: intervalSlider.leftPadding + intervalSlider.visualPosition * (intervalSlider.availableWidth - width)
                                    y: intervalSlider.topPadding + intervalSlider.availableHeight / 2 - height / 2
                                    width: 16; height: 16; radius: 8
                                    color: intervalSlider.pressed ? "#4a9eff" : "#ccc"
                                }
                            }
                            Label {
                                text: dlg.cursorReleaseInterval + "ms"
                                color: "#aaa"
                                font.pixelSize: 12
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Label {
                                text: dlg.cursorReleaseInterval <= 30 ? "(Aggressive)" : (dlg.cursorReleaseInterval <= 80 ? "(Balanced)" : "(Gentle)")
                                color: dlg.cursorReleaseInterval <= 30 ? "#ff8833" : (dlg.cursorReleaseInterval <= 80 ? "#4a9eff" : "#888")
                                font.pixelSize: 11
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }

                        // Status
                        Rectangle {
                            width: parent.width
                            height: 32
                            color: "transparent"
                            visible: dlg.statusMessage !== ""

                            Label {
                                anchors.centerIn: parent
                                text: dlg.statusMessage
                                color: dlg.statusMessage.indexOf("Failed") >= 0 ? "#ff4444" : "#00cc44"
                                font.pixelSize: 12
                                font.bold: true
                            }
                        }

                        // Help text
                        Rectangle {
                            width: parent.width
                            height: helpCol.implicitHeight + 16
                            color: "#1a1a2a"
                            border.color: "#334"
                            border.width: 1
                            radius: 6

                            Column {
                                id: helpCol
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 4

                                Label {
                                    text: "How It Works"
                                    color: "#4a9eff"
                                    font.pixelSize: 12
                                    font.bold: true
                                }
                                Label {
                                    text: "1. Select the game window from the dropdown\n" +
                                          "2. Click the green button to apply borderless mode + free the cursor\n" +
                                          "3. Your game fills the screen without borders, and your cursor can reach Nimbus\n" +
                                          "4. When done, click \"Restore Window & Stop\" to undo everything\n\n" +
                                          "Note: This works with games that use ClipCursor (most indie/older games).\n" +
                                          "FPS games using Raw Input (Valorant, CS2) are not supported — see Compatibility tab."
                                    color: "#999"
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                    width: parent.width
                                }
                            }
                        }
                    }
                }
            }

            // ===== Tab 1: Game Compatibility =====
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    Label {
                        text: "Game Compatibility Database"
                        color: "#ccc"
                        font.pixelSize: 14
                        font.bold: true
                    }

                    // Filter row
                    Row {
                        spacing: 6
                        Repeater {
                            model: [
                                { label: "All", filter: "" },
                                { label: "Verified", filter: "verified" },
                                { label: "Likely", filter: "likely" },
                                { label: "Partial", filter: "partial" },
                                { label: "Incompatible", filter: "incompatible" }
                            ]
                            delegate: Basic.Button {
                                property string filterVal: modelData.filter
                                text: modelData.label
                                onClicked: compatFilter.text = filterVal
                                contentItem: Label {
                                    text: parent.text
                                    color: compatFilter.text === parent.filterVal ? "white" : "#aaa"
                                    font.pixelSize: 11
                                    font.bold: compatFilter.text === parent.filterVal
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                                background: Rectangle {
                                    implicitWidth: 80
                                    implicitHeight: 26
                                    color: compatFilter.text === parent.filterVal ? "#3b82f6" : "#333"
                                    border.color: "#555"
                                    border.width: compatFilter.text === parent.filterVal ? 0 : 1
                                    radius: 4
                                }
                            }
                        }

                        // Hidden filter state
                        Label { id: compatFilter; text: ""; visible: false }
                    }

                    // Legend
                    Row {
                        spacing: 16
                        Repeater {
                            model: [
                                { label: "Verified", color: "#00cc44" },
                                { label: "Likely", color: "#4a9eff" },
                                { label: "Partial", color: "#ff8833" },
                                { label: "Incompatible", color: "#ff4444" }
                            ]
                            delegate: Row {
                                spacing: 4
                                Rectangle { width: 10; height: 10; radius: 5; color: modelData.color; anchors.verticalCenter: parent.verticalCenter }
                                Label { text: modelData.label; color: "#999"; font.pixelSize: 10; anchors.verticalCenter: parent.verticalCenter }
                            }
                        }
                    }

                    // Game list
                    ListView {
                        id: gameListView
                        width: parent.width
                        height: parent.height - 80
                        clip: true
                        spacing: 4
                        model: {
                            var filtered = []
                            for (var i = 0; i < dlg.gameCompat.length; i++) {
                                var g = dlg.gameCompat[i]
                                if (compatFilter.text === "" || g.status === compatFilter.text)
                                    filtered.push(g)
                            }
                            return filtered
                        }
                        Basic.ScrollBar.vertical: Basic.ScrollBar { policy: Basic.ScrollBar.AsNeeded }

                        delegate: Rectangle {
                            width: gameListView.width
                            height: gameItemCol.implicitHeight + 16
                            color: gameItemMa.containsMouse ? "#303030" : "#252525"
                            border.color: "#3a3a3a"
                            border.width: 1
                            radius: 4

                            MouseArea {
                                id: gameItemMa
                                anchors.fill: parent
                                hoverEnabled: true
                            }

                            Column {
                                id: gameItemCol
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 4

                                Row {
                                    spacing: 8
                                    Rectangle {
                                        width: 10; height: 10; radius: 5
                                        anchors.verticalCenter: parent.verticalCenter
                                        color: {
                                            if (modelData.status === "verified") return "#00cc44"
                                            if (modelData.status === "likely") return "#4a9eff"
                                            if (modelData.status === "partial") return "#ff8833"
                                            return "#ff4444"
                                        }
                                    }
                                    Label {
                                        text: modelData.name
                                        color: "white"
                                        font.pixelSize: 13
                                        font.bold: true
                                    }
                                    Label {
                                        text: "[" + modelData.status.toUpperCase() + "]"
                                        color: {
                                            if (modelData.status === "verified") return "#00cc44"
                                            if (modelData.status === "likely") return "#4a9eff"
                                            if (modelData.status === "partial") return "#ff8833"
                                            return "#ff4444"
                                        }
                                        font.pixelSize: 10
                                        font.bold: true
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                    Label {
                                        text: "Input: " + modelData.inputMethod
                                        color: "#888"
                                        font.pixelSize: 10
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                }

                                Label {
                                    text: modelData.notes
                                    color: "#aaa"
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                    width: parent.width
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    footer: Row {
        spacing: 12
        padding: 12
        layoutDirection: Qt.RightToLeft

        Basic.Button {
            text: "Close"
            onClicked: dlg.close()
            contentItem: Label {
                text: parent.text
                color: "#ccc"
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                implicitWidth: 80
                implicitHeight: 32
                color: parent.down ? "#444" : "#333"
                border.color: "#555"
                border.width: 1
                radius: 4
            }
        }
    }

    onOpened: {
        dlg._refreshWindows()
        dlg._loadCompat()
        dlg._autoDetect()
        // Sync cursor release state
        if (controller) {
            dlg.cursorReleaseOn = controller.isCursorReleaseActive()
        }
    }

    // Internal functions
    function _refreshWindows() {
        if (!controller) return
        try {
            dlg.windowList = controller.getWindowList()
            // Restore selection if hwnd still exists
            if (dlg.selectedHwnd > 0) {
                for (var i = 0; i < dlg.windowList.length; i++) {
                    if (dlg.windowList[i].hwnd === dlg.selectedHwnd) {
                        windowCombo.currentIndex = i
                        return
                    }
                }
            }
        } catch (e) {
            console.log("_refreshWindows error:", e)
        }
    }

    function _loadCompat() {
        if (!controller) return
        try {
            dlg.gameCompat = controller.getGameCompatList()
        } catch (e) {
            console.log("_loadCompat error:", e)
        }
    }

    function _autoDetect() {
        if (!controller) return
        try {
            var data = controller.autoDetectGame()
            if (data && data.gameName) {
                dlg.autoDetectedGame = data.gameName

                // Auto-select the detected window
                for (var i = 0; i < dlg.windowList.length; i++) {
                    if (dlg.windowList[i].hwnd === data.hwnd) {
                        windowCombo.currentIndex = i
                        dlg.selectedHwnd = data.hwnd
                        dlg.selectedTitle = data.title
                        break
                    }
                }

                // Set recommended interval
                if (data.recommendedInterval) {
                    dlg.cursorReleaseInterval = data.recommendedInterval
                    intervalSlider.value = data.recommendedInterval
                }

                dlg.statusMessage = data.gameName + " detected — " + data.status
            } else {
                dlg.autoDetectedGame = ""
            }
        } catch (e) {
            console.log("_autoDetect error:", e)
            dlg.autoDetectedGame = ""
        }
    }
}
