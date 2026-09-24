using UnrealBuildTool;

public class PsychoVertexSceneReconstructor : ModuleRules
{
    public PsychoVertexSceneReconstructor(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PrivateDependencyModuleNames.AddRange(new[]
        {
            "Core", "CoreUObject", "Engine", "Slate", "SlateCore", "InputCore",
            "UnrealEd", "LevelEditor", "ToolMenus", "DesktopPlatform",
            "AssetRegistry", "ContentBrowser", "Json", "JsonUtilities"
        });
    }
}
