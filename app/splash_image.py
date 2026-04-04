"""
Standalone splash screen subprocess for Vibe Player.

Displays a borderless, topmost window with a centered image. Intended to be
launched as ``python splash_image.py <image_path>`` before the main application,
or from a frozen build via ``VibePlayer.exe --vibe-splash <image_path>``.
"""

import logging
import sys
import tkinter as tk

from PIL import Image, ImageTk


def run_splash(img_path: str) -> None:
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="black")

    image = Image.open(img_path)
    img_w, img_h = image.size
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x = (screen_w - img_w) // 2
    y = (screen_h - img_h) // 2
    root.geometry(f"{img_w}x{img_h}+{x}+{y}")
    photo = ImageTk.PhotoImage(image)

    label = tk.Label(root, image=photo, borderwidth=0, highlightthickness=0, bg="black")
    label.image = photo
    label.pack()

    def safe_exit(*args: object, **kwargs: object) -> None:
        try:
            root.attributes("-topmost", False)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", safe_exit)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        safe_exit()
    except Exception:
        safe_exit()
    finally:
        safe_exit()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logging.info("Usage: python splash_image.py <path_to_image.png>")
        sys.exit(1)
    run_splash(sys.argv[1])
