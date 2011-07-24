
import Qt 4.7

import 'config.js' as Config

Item {
    id: episodeList

    property alias model: listView.model
    property alias moving: listView.moving

    signal episodeContextMenu(variant episode)

    ListView {
        id: listView
        anchors.fill: parent
        property bool closeAll: false

        delegate: Item {
            property bool closeAll: listView.closeAll

            height: Config.listItemHeight
            width: parent.width

            onCloseAllChanged: {
                if (closeAll) hideActions()
            }

            function showActions() {
                if (height != Config.listItemHeight) {
                    hideActions()
                    return
                }
                listView.closeAll = true
                listView.closeAll = false
                height = Config.listItemHeight + Config.smallSpacing * 3 + Config.headerHeight
                episodeItem.y = Config.smallSpacing

                loader.source = 'EpisodeActions.qml'
                loader.item.episode = modelData
            }

            function hideActions() {
                loader.source = ''
                height = Config.listItemHeight
                episodeItem.y = 0
            }

            Loader {
                id: loader
                clip: true

                Behavior on opacity { PropertyAnimation { } }

                opacity: ((source != '')?1:0)

                anchors {
                    left: parent.left
                    right: parent.right
                    top: parent.top
                    topMargin: episodeItem.y + episodeItem.height
                    bottom: parent.bottom
                }

                width: parent.width
            }

            Behavior on height { PropertyAnimation { } }

            EpisodeItem {
                id: episodeItem
                width: parent.width
                onSelected: parent.showActions()
                onContextMenu: episodeList.episodeContextMenu(item)

                Behavior on y { PropertyAnimation { } }
            }
        }

        header: Item { height: Config.headerHeight }
        footer: Item { height: Config.headerHeight }
    }
}

