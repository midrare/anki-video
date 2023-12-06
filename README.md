# anki-video

Inline video player support for Anki.

![Animated demo](demo.gif)


## Getting Started

### Prerequisites
 
  1. Go to Anki -> Tools (menubar) -> Add-ons -> Install from file  
  2. Select file downloaded from releases page  

## Usage
Drag and drop video files into any of the fields in the "Add card" window. Use only `.webm` files. Other formats need to be pre-converted before dropping into a note.  

Video player options can be changed on a global or per-video basis. For global options, click the "config" button in the Anki addons window. For video-specific options, click the `</>` button in the note editor window, find the `<video>` tag, and edit the `<config>` values.

## License
Distributed under the MIT license. See `LICENSE.txt` for more information


## Acknowledgements
Uses [video.js](https://github.com/videojs/video.js), included under the Apache 2.0 license. See the top of the video.js-related files for license details.
