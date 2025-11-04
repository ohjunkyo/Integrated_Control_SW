// config2.h

#ifndef CONFIG2_H
#define CONFIG2_H

#include <string>

const std::string BasePath = "/home/precalkor/ADC/ADC_test";

const std::string DaqProgramPath = "/home/precalkor/ADC/ADC_test/";
const std::string RawDataPath = "/home/precalkor/ADC/ADC_test/Data/RAW/";
const std::string ProcessedDataPath = "/home/precalkor/ADC/ADC_test/Data/production/";
const std::string FinalResultPath = "/home/precalkor/ADC/ADC_test/Data/FinalResult/";
const std::string ImagePath = "/home/precalkor/ADC/ADC_test/Data/image/";
const std::string LogDir = "/home/precalkor/ADC/ADC_test/LOG/DAQ/";

// --- DAQ Common Settings --- //
const int Events = 1000;
const int TimeWindow = 16834;
const int PostTrigger = 80;
const std::string ChannelMask = "00000111";

// --- Sequence Settings --- //
const int NumSequences = 1;
const int IntervalTime = 30;

// -- Laser (Current, mA) --- //
const std::string Laser = "126";

// --- PMT configuration --- //
const std::string SN1 = "ED1950";
const std::string direction1 = "A";
const std::string SN2 = "EL9590";
const std::string direction2 = "B";
const std::string SN3 = "";
const std::string direction3 = "";

// -- Angle Configuration --- //
const std::string RotateAngle1 = "0";
const std::string TiltAngle1 = "0";
const std::string RotateAngle2 = "0";
const std::string TiltAngle2 = "0";

// --- High Voltage --- //
const std::string HV1 = "1750";
const std::string HV2 = "1750";
const std::string HV3 = "";

// --- NOTE ---
const std::string NOTE = "shjeondarktest";

// --- Trigger Channel ---
const int TriggerCh = 2;
                         // It is analogue

// --- Shift Information ---
const std::string Shift_worker = "";
const std::string Expert = "";

// It is monitor PMT
const std::string RotateAngle3 = "";
const std::string TiltAngle3 = "0";


#endif // CONFIG2_H
