# Third-party notices

`alfred/hotkey.py` is adapted from code published under the MIT License.

```
MIT License

Copyright (c) 2026 Abdur Rahim
Copyright (c) 2026 VectorDash contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## llama.cpp

Alfred runs search by meaning and the answer model on this PC with `llama-server.exe` from
[llama.cpp](https://github.com/ggml-org/llama.cpp) (build b12318), which is published under the MIT License. The engine
is not part of this repository. When it is shipped with Alfred, its own `LICENSE` file (and the `LICENSE-LLVM-OpenMP`
file that comes with the Windows build, for `libomp.dll`) must be shipped with it and its copyright line copied here
from that file.

## Models

The model files are not part of this repository. EmbeddingGemma 2 (search) and gemma3:4b (answers on this PC) are
published by Google; their terms (the Gemma Terms of Use, or Apache-2.0 if the model card says so) must be checked on
each model card before the files are offered for download.

## Provider logos

`alfred/assets/logos/` holds the Claude, Anthropic and OpenAI marks as SVGs from
[Simple Icons](https://simpleicons.org) (simple-icons 13.21.0, CC0 1.0). The logos are trademarks of their
respective owners and are used only to identify which provider answers in Alfred; no endorsement is implied.
`pc.svg` (the "On this PC" mark) was drawn for Alfred.
