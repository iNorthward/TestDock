# PLATFORM 自托管字体

面板主题使用的 Inter / JetBrains Mono / Montserrat / Cormorant Garamond，全部打包在仓库内，**不依赖 Google Fonts CDN**。

更新字体文件（需联网）：

```bash
cd /tmp && rm -rf platform-font-fetch && mkdir platform-font-fetch && cd platform-font-fetch
npm pack @fontsource-variable/inter@5.1.1
tar -xzf fontsource-variable-inter-5.1.1.tgz
cp package/files/inter-latin-wght-normal.woff2 ../../assets/fonts/inter-latin.woff2
# 其余字体同理，见 package/files/
```

许可：SIL Open Font License 1.1（各字体上游许可）
