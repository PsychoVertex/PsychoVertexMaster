#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FPVMSceneSchemaTest,
    "PsychoVertex.SceneReconstructor.SchemaAndTransform",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FPVMSceneSchemaTest::RunTest(const FString& Parameters)
{
    const FString Text = TEXT(R"({
        "schema":"pvm.unreal_scene","version":1,
        "scene":{"reconstruction_id":"abc"},
        "assets":[{"id":"Bench","static_mesh_name":"SM_Bench"}],
        "placements":[{"id":"p1","asset_id":"Bench","kind":"canonical","source_batch":1,
          "transform":{"location_cm":[100,-200,300],"rotation_xyzw":[0,0,0,1],"scale":[1,1,1]}}]
    })");
    TSharedPtr<FJsonObject> Root;
    TestTrue(TEXT("Valid JSON parses"), FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Root));
    TestEqual(TEXT("Schema"), Root->GetStringField(TEXT("schema")), FString(TEXT("pvm.unreal_scene")));
    TestEqual(TEXT("Version"), Root->GetIntegerField(TEXT("version")), 1);
    const TArray<TSharedPtr<FJsonValue>>& Placements = Root->GetArrayField(TEXT("placements"));
    const TSharedPtr<FJsonObject> Transform = Placements[0]->AsObject()->GetObjectField(TEXT("transform"));
    TestEqual(TEXT("Location has three values"), Transform->GetArrayField(TEXT("location_cm")).Num(), 3);
    TestEqual(TEXT("Quaternion has four values"), Transform->GetArrayField(TEXT("rotation_xyzw")).Num(), 4);
    return true;
}

#endif
