# The app uses no reflection, so the default rules plus the libraries' own
# consumer rules are enough. These two exist only to keep crash reports and
# protocol classes readable if anyone ever inspects a release build.
-keep class com.eteqcam.net.** { *; }
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
