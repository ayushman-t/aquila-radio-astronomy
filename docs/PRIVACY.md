# Privacy

Aquila Radio Astronomy does not collect, store or send any personal information. There is no account, no telemetry, no analytics and no crash reporting.

Everything the app records stays on your computer, in the data folder you choose: spectra, hit tables, plots and the configuration file.

The app uses the network only when you ask it to:

- When you add a remote node by address, the app fetches that node's files from the address you typed and nothing else.
- When you press the sky map background button, the app downloads one all-sky image from the CDS hips2fits service at the University of Strasbourg and keeps it in your cache. That request carries no information about you beyond what any web request carries.

No other connections are made. The source code is public at https://github.com/ayushman-t/aquila-radio-astronomy so this can be checked.

Ayushman Tripathi, www.radioastronomy.in
