# SakuraMediaBE

[简体中文](./README.zh-CN.md) | English

SakuraMediaBE is the server-side project of SakuraMedia. It provides the backend capabilities of SakuraMedia: media library management, movie metadata, download tasks, video thumbnail generation, moment marking, image-based search, and playback access. The client-side project lives in [sakuramedia](https://github.com/tinypinglite/sakuramedia).

The project is currently built on Python 3.10, FastAPI, Peewee, Pydantic 2 and APScheduler. The code is layered as `api -> service -> model` and targets single-account deployments.

## Plugin development

Plugin developers can find runnable examples, the manifest JSON Schema and type stubs in [`plugin-sdk/`](./plugin-sdk/README.md). The full plugin contract is documented in the [plugin development guide](https://tinypinglite.github.io/sakuramedia/guide/plugin-development).

## Acknowledgements

The movie number parsing implementation and its regression tests were inspired by the following open-source projects, and we would like to thank their authors: [JavSP](https://github.com/Yuukiy/JavSP), [JavBoss](https://github.com/Solr159/JavBoss), [Emby.Plugins.JavScraper](https://github.com/JavScraper/Emby.Plugins.JavScraper), [JAVOneStop](https://github.com/ddd354/JAVOneStop), [metatube-sdk-go](https://github.com/metatube-community/metatube-sdk-go), [Movie_Data_Capture](https://github.com/mvdctop/Movie_Data_Capture), [mdcz](https://github.com/ShotHeadman/mdcz), [javinizer-go](https://github.com/javinizer/javinizer-go).

## Risk notice

* This project is for technical exchange only.
* It does not provide any movie resources, download links, or similar content.
* Users bear all consequences of using this tool; the author is not liable.
* Please read and comply with the laws and regulations of your jurisdiction before using this software. If using this software violates applicable laws or regulations, do not use it.
* Commercial use of this software is prohibited.
* If you do not agree with any of the terms above, do not use this software.

For any infringement concerns, please contact tinyping@protonmail.com by email.

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](./LICENSE); non-commercial use only.

Copyright (C) 2026 tinypinglite
