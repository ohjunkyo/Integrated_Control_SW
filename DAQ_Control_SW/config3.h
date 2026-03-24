// config3.h

#ifndef CONFIG3_H
#define CONFIG3_H

#include <string>

const std::string BasePath = "/home/precalkor/ADC/ADC_test";

const std::string DaqProgramPath = "/home/precalkor/ADC/ADC_test/";
const std::string RawDataPath = "/home/precalkor/ADC/ADC_test/Data/RAW/";
const std::string ProcessedDataPath = "/home/precalkor/ADC/ADC_test/Data/production/";
const std::string FinalResultPath = "/home/precalkor/ADC/ADC_test/Data/FinalResult/";
const std::string ExternalPath = "/home/precalkor/external_HDD_1_4T/Data_Backup/RAW/";
const std::string ImagePath = "/home/precalkor/ADC/ADC_test/Data/image/";
const std::string LogDir = "/home/precalkor/ADC/ADC_test/LOG/DAQ/";

// --- DAQ Common Settings --- //
const int Events = 200000;
const int TimeWindow = 1024;
const int PostTrigger = 60;
const std::string ChannelMask = "00001111";

// --- Sequence Settings --- //
const int NumSequences = 1;
const int IntervalTime = 0;

// -- Laser (Current, mA) --- //
const std::string Laser = "133";
const std::string Wavelength = "405";

// --- PMT configuration --- //
// Ch0 - monitor, Ch1 - PMT 1, Ch2 - PMT 2 same as High voltage setting
const std::string SN1 = "EM2740";
const std::string direction1 = "A";
const std::string SN2 = "EL1635";
const std::string direction2 = "B";
const std::string SN3 = "EL9590";
const std::string direction3 = "B";

// -- Angle Configuration --- //
const std::string RotateAngle1 = "0";
const std::string TiltAngle1 = "0";

const std::string RotateAngle2 = "45";
const std::string TiltAngle2 = "0";

const std::string RotateAngle3 = "45";
const std::string TiltAngle3 = "0";

// --- High Voltage --- //
const std::string HV1 = "1670";
const std::string HV2 = "1840";
const std::string HV3 = "1770";

// --- NOTE ---
const std::string NOTE = "test";

// --- Trigger Channel ---
const int TriggerCh = 3;

// --- Shift Information ---
const std::string Shift_worker = "Junkyo";
const std::string Expert = "Junkyo";


#endif // CONFIG3_H
