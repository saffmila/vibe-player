module.exports = {
  // Pinokio launcher schema (see https://desktop.pinokio.co/docs/)
  version: "8.0.0",
  title: "Vibe Player",
  description: "Local video manager with AI autotagging, powered by VLC",
  icon: "icon.png",
  // VLC cannot be installed by Pinokio; show it before Install
  pre: [{
    title: "VLC Media Player (64-bit)",
    description: "Required for video playback. Install the 64-bit build from videolan.org before starting Vibe Player.",
    href: "https://www.videolan.org/vlc/"
  }],
  menu: async (kernel, info) => {
    // Installed when the Pinokio-managed Python venv exists at project root
    let installed = info.exists("env")
    let running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js")
    }

    if (running.install) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Installing",
        href: "install.js"
      }]
    }

    if (running.update) {
      return [{
        default: true,
        icon: "fa-solid fa-terminal",
        text: "Updating",
        href: "update.js"
      }]
    }

    if (installed) {
      if (running.start) {
        return [{
          default: true,
          icon: "fa-solid fa-terminal",
          text: "Terminal",
          href: "start.js"
        }]
      }

      return [{
        default: true,
        icon: "fa-solid fa-power-off",
        text: "Start Vibe Player",
        href: "start.js"
      }, {
        icon: "fa-solid fa-arrows-rotate",
        text: "Update",
        href: "update.js"
      }, {
        icon: "fa-solid fa-plug",
        text: "Install",
        href: "install.js"
      }]
    }

    return [{
      default: true,
      icon: "fa-solid fa-plug",
      text: "Install",
      href: "install.js"
    }]
  }
}
