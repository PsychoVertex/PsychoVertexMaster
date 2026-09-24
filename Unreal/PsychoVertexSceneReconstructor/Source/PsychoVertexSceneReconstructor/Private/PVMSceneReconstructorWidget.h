#pragma once

#include "CoreMinimal.h"
#include "Widgets/SCompoundWidget.h"

class SEditableTextBox;
class SMultiLineEditableTextBox;
class UStaticMesh;

struct FPVMPlacement
{
    FString Id;
    FString AssetId;
    FString Kind;
    int32 SourceBatch = 0;
    FTransform Transform;
};

struct FPVMValidatedScene
{
    FString ReconstructionId;
    TMap<FString, UStaticMesh*> MeshesByAssetId;
    TMap<FString, FString> MeshNamesByAssetId;
    TArray<FPVMPlacement> Placements;

    void Reset()
    {
        ReconstructionId.Reset();
        MeshesByAssetId.Reset();
        MeshNamesByAssetId.Reset();
        Placements.Reset();
    }
};

class SPVMSceneReconstructorWidget final : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(SPVMSceneReconstructorWidget) {}
    SLATE_END_ARGS()

    void Construct(const FArguments& InArgs);

private:
    FReply BrowseJson();
    FReply UseSelectedContentFolder();
    FReply Validate();
    FReply Reconstruct();
    FReply DeletePVMFolderActors();
    void InvalidateValidation(const FText&);
    bool ValidateInputs(FString& OutSummary);
    void SetStatus(const FString& Message);

    TSharedPtr<SEditableTextBox> AssetFolderText;
    TSharedPtr<SEditableTextBox> JsonPathText;
    TSharedPtr<SMultiLineEditableTextBox> StatusText;
    FPVMValidatedScene Validated;
    bool bValidationSucceeded = false;
};
