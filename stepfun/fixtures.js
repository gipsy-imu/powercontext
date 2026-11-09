// Original example notes. Titles are also the relevance labels for the benchmark.
export const documents = [
  ['Thermal probe calibration', 'To measure sensor drift, submerge the temperature probe in a stirred ice bath. Compare the display against a certified reference thermometer. Record the correction factor, ambient temperature and calibration date in the instrument log.'],
  ['Bridge circuit troubleshooting', 'Measure the excitation voltage at the Wheatstone bridge terminals. Swap the two sense leads and verify that the sign of the differential measurement reverses. Inspect the solder joints under magnification.'],
  ['Fermentation sampling', 'Collect a sterile aliquot every two hours. Record optical density at 600 nm and the culture temperature. Use an uninoculated broth blank before measuring each new batch.'],
  ['Spectrophotometer blank', 'Fill a clean quartz cuvette with solvent only. Zero the absorbance before inserting the analyte. Keep the polished faces oriented toward the beam and wipe off fingerprints.'],
  ['Gradient descent baseline', 'Train a linear regression model with a fixed learning rate. Plot training loss against iteration number. Standardize features, use a held-out validation set and report mean squared error.'],
  ['Emergency eyewash inspection', 'Activate the emergency eyewash station weekly. Check both water streams for balanced flow and note any clogged nozzles. Keep the path clear and log each inspection.'],
  ['Chromatography column wash', 'Flush the column with the recommended mobile phase before use. After the final injection, remove residual sample with a solvent gradient and store the column in its approved solution.'],
  ['Mass balance verification', 'Tare the balance with an empty weigh boat and verify readings with a certified reference mass. Close the draft shield while measuring and log the measured deviation.'],
  ['Vibration sensor mounting', 'Bolt the accelerometer to the motor case. Run the motor at constant speed and record a spectrum to identify resonance and bearing defects. Photograph the mounting orientation.'],
  ['Microcontroller serial capture', 'Set the UART to 115200 baud with eight data bits and no parity. Save raw serial output to a timestamped file and check the wiring for a shared ground.'],
  ['Pipette volume check', 'Dispense water onto a tared analytical balance and convert the mass to volume using water density. Repeat ten times per volume setting to estimate precision and bias.'],
  ['Microscope image alignment', 'Use a stage micrometer to calibrate pixel spacing. Adjust focus before recording each frame and keep exposure fixed so cells can be compared between specimens.'],
].map(([title, text]) => ({ title, text }));

export const queries = [
  ['sensor calibration with an ice bath', 'Thermal probe calibration'],
  ['reverse the signal from a Wheatstone bridge', 'Bridge circuit troubleshooting'],
  ['cell growth optical density', 'Fermentation sampling'],
  ['zero the absorbance with solvent', 'Spectrophotometer blank'],
  ['record raw data over UART', 'Microcontroller serial capture'],
  ['water density measurement for pipette accuracy', 'Pipette volume check'],
  ['convert microscope pixels to physical distance', 'Microscope image alignment'],
  ['clogged nozzles in an emergency wash station', 'Emergency eyewash inspection'],
];
